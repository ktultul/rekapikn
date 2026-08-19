import os
import re
import json
import time
import sqlite3
from functools import wraps

from dotenv import load_dotenv
load_dotenv()  # baca file .env di folder ini dan isi ke os.environ, kalau ada

from flask import Flask, render_template, request, redirect, url_for, session, flash
import gspread
from google.oauth2.service_account import Credentials

# ----------------------------------------------------------------------------
# Konfigurasi (diambil dari environment variables — lihat .env.example)
# ----------------------------------------------------------------------------
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "")
SERVICE_ACCOUNT_FILE = os.environ.get("SERVICE_ACCOUNT_FILE", "service_account.json")
# Alternatif untuk platform tanpa upload file rahasia (mis. Vercel): isi
# SELURUH ISI service_account.json ke env var ini (as-is, satu baris JSON).
SERVICE_ACCOUNT_JSON = os.environ.get("SERVICE_ACCOUNT_JSON", "").strip()
SECRET_KEY = os.environ.get("SECRET_KEY", "ganti-ini-di-production")
DB_PATH = os.environ.get("DB_PATH", "users.db")
# Alternatif untuk platform tanpa disk permanen (mis. Vercel): isi daftar PIN
# sebagai JSON lewat env var ini, bukan lewat file users.db. Format:
# {"123456": {"sheet_key": "KondomBocor", "display_name": "KondomBocor"}, ...}
# Generate isinya lewat: python manage_pins.py export-json
PINS_JSON = os.environ.get("PINS_JSON", "").strip()
CACHE_SECONDS = int(os.environ.get("CACHE_SECONDS", "60"))
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_SECONDS = 300

# Kalau AUTO_DETECT_WEEKS aktif (default), app otomatis mendeteksi semua tab
# yang cocok pola "BEYOND (Week <angka>)" tiap kali data di-refresh — jadi
# minggu baru (Week 7, Week 8, dst) langsung muncul begitu tab-nya dibuat
# di Google Sheets, tanpa perlu ubah kode / .env / restart app.
#
# Kalau mau kontrol manual (misal urutan/nama beda), isi SHEET_TABS di .env
# untuk override total — kalau SHEET_TABS diisi, auto-detect otomatis
# dimatikan dan app pakai persis daftar itu.
AUTO_DETECT_WEEKS = os.environ.get("AUTO_DETECT_WEEKS", "true").strip().lower() not in ("0", "false", "no")

# Pola nama tab minggu yang dikenali otomatis. {n} diganti regex angka.
WEEK_TAB_REGEX = re.compile(r"^BEYOND\s*\(\s*Week\s*(\d+)\s*\)$", re.IGNORECASE)

# Tab non-minggu yang selalu ikut ditampilkan (kalau memang ada di spreadsheet),
# ditaruh setelah semua tab minggu. Format sama seperti SHEET_TABS.
DEFAULT_EXTRA_TABS = [("Yang Bakal Cair", "Yang Bakal Cair")]

# Fallback kalau auto-detect dimatikan DAN SHEET_TABS tidak diisi.
DEFAULT_TABS = [
    ("Week 1", "BEYOND (Week 1)"),
    ("Week 2", "BEYOND (Week 2)"),
    ("Week 3", "BEYOND (Week 3)"),
    ("Week 4", "BEYOND (Week 4)"),
    ("Week 5", "BEYOND (Week 5)"),
    ("Week 6", "BEYOND (Week 6)"),
    ("Yang Bakal Cair", "Yang Bakal Cair"),
]


def parse_tab_list(raw):
    """Parse format 'Label|Nama Tab;Label2|Nama Tab 2' -> [(label, title), ...]"""
    tabs = []
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "|" in chunk:
            label, title = chunk.split("|", 1)
        else:
            label = title = chunk
        tabs.append((label.strip(), title.strip()))
    return tabs


SHEET_TABS_OVERRIDE = os.environ.get("SHEET_TABS", "").strip()
EXTRA_TABS = parse_tab_list(os.environ.get("SHEET_EXTRA_TABS", "")) or DEFAULT_EXTRA_TABS

# Kalau SHEET_TABS diisi manual, itu menang mutlak (auto-detect dimatikan).
TABS = parse_tab_list(SHEET_TABS_OVERRIDE) if SHEET_TABS_OVERRIDE else None
if TABS:
    AUTO_DETECT_WEEKS = False
elif not AUTO_DETECT_WEEKS:
    TABS = DEFAULT_TABS
# kalau AUTO_DETECT_WEEKS True dan SHEET_TABS kosong, TABS tetap None —
# daftar tab dihitung ulang tiap request lewat get_active_tabs() di bawah.


# Kolom "Nama - Nick" dipakai sebagai identitas utama untuk mencocokkan
# PIN/login ke baris data di tiap tab. Header sheet ini punya banyak kolom
# kosong di kanan (tidak unik), jadi kita baca nilai mentah (get_all_values)
# dan parse manual — bukan lewat get_all_records() yang mensyaratkan header unik.


def normalize(s):
    """Samakan nama untuk dicocokkan: buang isi dalam kurung, buang karakter
    non-alfanumerik, lowercase. 'KYE - Axile (P)' dan 'kye axile' -> sama."""
    s = s or ""
    s = re.sub(r"\([^)]*\)", "", s)
    s = re.sub(r"\[[^\]]*\]", "", s)
    s = re.sub(r"[^a-zA-Z0-9]", "", s)
    return s.lower().strip()


def is_empty_row(row):
    return all(not (c or "").strip() for c in row)


def col_letter_to_index(letter):
    """'A'->0, 'B'->1, ..., 'L'->11, ..., 'Z'->25, 'AA'->26, dst."""
    letter = letter.strip().upper()
    result = 0
    for ch in letter:
        result = result * 26 + (ord(ch) - ord("A") + 1)
    return result - 1


# Section "PERSENTASE" dikonfirmasi selalu ada di kolom L-P (Elan%, CM%,
# Markas%, Total%, Final%), apa pun isi teks judul/header di sana — jadi kita
# pakai posisi kolom tetap (bukan tebak dari teks) supaya tidak meleset kalau
# format judulnya beda-beda antar tab.
PCT_COL_ELAN = col_letter_to_index("L")
PCT_COL_CM = col_letter_to_index("M")
PCT_COL_MARKAS = col_letter_to_index("N")
PCT_COL_TOTAL = col_letter_to_index("O")
PCT_COL_FINAL = col_letter_to_index("P")

# Kolom utama (No s.d. Alasan) berhenti di kolom K (index 10) — dipakai untuk
# membatasi map_columns() supaya tidak salah cocok dengan header L-P yang
# teksnya bisa jadi sama persis ("PB ELAN" dst muncul dua kali di baris itu).
MAIN_TABLE_END_COL = col_letter_to_index("K") + 1  # exclusive bound


def map_columns(header_row, cont_row):
    """Cari index kolom berdasar kata kunci di baris header (+ baris
    lanjutan kalau header-nya 2 baris karena merged cell). header_row yang
    dikirim ke sini sudah dipotong sampai sebelum section PERSENTASE (kalau
    ada), supaya tidak salah cocok dengan kolom yang teksnya sama di sana."""
    n = len(header_row)
    combined = []
    for i in range(n):
        h = header_row[i] or ""
        c = (cont_row[i] if cont_row and i < len(cont_row) else "") or ""
        combined.append((h + " " + c).strip().lower())

    cols = {}
    name_count = 0
    for i, h in enumerate(combined):
        if "no" not in cols and (header_row[i] or "").strip().lower() == "no":
            cols["no"] = i
            continue
        if "nama" in h:
            name_count += 1
            if name_count == 1:
                cols["nameIKN"] = i
            elif name_count == 2:
                cols["nameBangsa"] = i
            continue
        if "pbElan" not in cols and ("pb elan" in h or "elan" in h):
            cols["pbElan"] = i
            continue
        if "pbCM" not in cols and ("pb cm" in h or re.search(r"\bcm\b", h)):
            cols["pbCM"] = i
            continue
        if "pbMarkas" not in cols and ("pb markas" in h or "markas" in h):
            cols["pbMarkas"] = i
            continue
        if "admin" not in cols and "admin" in h:
            cols["admin"] = i
            continue
        if "rekap" in h:
            cols["rekap"] = i
            continue
        if "alasan" in h:
            cols["alasan"] = i
            continue
        if "final" not in cols and "final" in h:
            cols["final"] = i
            continue
        if "total" not in cols and (h == "total" or ("total" in h and "rekap" not in h)):
            cols["total"] = i
            continue
    return cols


def rgb_of(cell_format):
    """Ambil (r,g,b) 0-255 dari dict effectiveFormat.backgroundColor Sheets API."""
    if not cell_format:
        return None
    r = int(round((cell_format.get("red", 0) or 0) * 255))
    g = int(round((cell_format.get("green", 0) or 0) * 255))
    b = int(round((cell_format.get("blue", 0) or 0) * 255))
    return (r, g, b)


def classify_final_color(rgb):
    """Klasifikasi warna background sel FINAL jadi status pembayaran.
    Toleransi kecil (total selisih channel <= 30) supaya warna yang sedikit
    beda karena rendering tetap kena deteksi, tapi putih/tanpa-warna tidak
    salah kena karena selisihnya jauh lebih besar dari toleransi ini."""
    if rgb is None:
        return "belum"
    r, g, b = rgb

    def close(target, tol=30):
        return abs(r - target[0]) + abs(g - target[1]) + abs(b - target[2]) <= tol

    if close((0, 255, 0)):
        return "cair"
    if close((255, 0, 0)):
        return "kas"
    if close((255, 255, 0)):
        return "tunda"
    return "belum"


def parse_tab(rows, color_rows=None):
    """rows = list of list of string (nilai tampilan tiap sel).
    color_rows = list sejajar berisi list (r,g,b) background tiap sel kolom
    FINAL saja tidak cukup — kita kirim warna background SEMUA sel supaya
    index kolomnya bisa disamakan dengan kolom FINAL yang baru ketemu
    setelah header di-parse."""
    if not rows:
        return []
    header_idx = -1
    for i, row in enumerate(rows):
        if any((c or "").strip().lower() == "no" for c in row):
            header_idx = i
            break
    if header_idx == -1:
        return []

    header_row = rows[header_idx]
    cont_row = None
    data_start = header_idx + 1
    if header_idx + 1 < len(rows):
        nxt = rows[header_idx + 1]
        if not nxt or not (nxt[0] or "").strip():
            cont_row = nxt
            data_start = header_idx + 2

    # Kolom utama dibatasi sampai kolom K supaya map_columns() tidak salah
    # cocok dengan header L-P yang teksnya bisa jadi sama persis
    # ("PB ELAN" dst muncul dua kali di baris yang sama).
    main_header_row = header_row[:MAIN_TABLE_END_COL]
    main_cont_row = cont_row[:MAIN_TABLE_END_COL] if cont_row is not None else None

    cols = map_columns(main_header_row, main_cont_row)
    if "nameIKN" not in cols:
        return []

    def get(row, key):
        idx = cols.get(key)
        if idx is None or idx >= len(row):
            return ""
        return row[idx]

    def get_col(row, idx):
        if idx >= len(row):
            return ""
        return row[idx] or ""

    final_idx = cols.get("final")

    records = []
    for i in range(data_start, len(rows)):
        row = rows[i]
        if is_empty_row(row):
            break
        name = get(row, "nameIKN").strip()
        if not name:
            continue

        final_rgb = None
        if color_rows and i < len(color_rows) and final_idx is not None:
            crow = color_rows[i]
            if final_idx < len(crow):
                final_rgb = crow[final_idx]

        records.append({
            "name": name,
            "norm": normalize(name),
            "pb_elan": get(row, "pbElan"),
            "pb_cm": get(row, "pbCM"),
            "pb_markas": get(row, "pbMarkas"),
            "admin": get(row, "admin"),
            "total": get(row, "total"),
            "final": get(row, "final"),
            "rekap": get(row, "rekap"),
            "alasan": get(row, "alasan"),
            "final_status": classify_final_color(final_rgb),
            "pb_elan_pct": get_col(row, PCT_COL_ELAN),
            "pb_cm_pct": get_col(row, PCT_COL_CM),
            "pb_markas_pct": get_col(row, PCT_COL_MARKAS),
            "total_pct": get_col(row, PCT_COL_TOTAL),
            "final_pct": get_col(row, PCT_COL_FINAL),
        })
    return records


STATUS_LABEL = {
    "cair": "Cair",
    "kas": "Masuk Kas / Tidak Cair",
    "tunda": "Tertunda / Cair Minggu Depan",
    "belum": "Belum Cair",
    "none": "Tidak Ada Data",
}

app = Flask(__name__)
app.secret_key = SECRET_KEY


def parse_pct_value(s):
    """'2,84%' -> 2.84 ; '-%' / '' / None -> None. Dipakai untuk menggambar
    ring persentase (SVG donut) di dashboard."""
    if not s:
        return None
    s = str(s).strip()
    if not s or s in ("-", "-%"):
        return None
    s = s.replace("%", "").strip().replace(",", ".")
    try:
        return max(0.0, min(100.0, float(s)))
    except ValueError:
        return None


@app.template_filter("pctnum")
def pctnum_filter(s):
    v = parse_pct_value(s)
    return v if v is not None else 0

# ----------------------------------------------------------------------------
# Database lokal PIN -> nama (terpisah dari Google Sheet)
# ----------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    # Kalau pakai PINS_JSON (mode read-only, misal di Vercel), tidak perlu
    # sqlite sama sekali — filesystem-nya toh tidak permanen di sana.
    if PINS_JSON:
        return
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            pin TEXT PRIMARY KEY,
            sheet_key TEXT NOT NULL,
            display_name TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()


_pins_json_cache = None


def find_user_by_pin(pin: str):
    """Cari user berdasar PIN. Kalau env var PINS_JSON diisi, baca dari situ
    (read-only, cocok untuk platform tanpa disk permanen kayak Vercel).
    Kalau tidak, pakai users.db (sqlite) seperti biasa — cocok untuk
    lokal/Render, dan mendukung penambahan user lewat manage_pins.py."""
    global _pins_json_cache
    if PINS_JSON:
        if _pins_json_cache is None:
            try:
                _pins_json_cache = json.loads(PINS_JSON)
            except json.JSONDecodeError:
                _pins_json_cache = {}
        entry = _pins_json_cache.get(pin)
        if not entry:
            return None
        return {"pin": pin, "sheet_key": entry.get("sheet_key", ""), "display_name": entry.get("display_name", "")}

    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE pin = ?", (pin,)).fetchone()
    conn.close()
    return row


# ----------------------------------------------------------------------------
# Akses Google Sheet (pakai service account) — cache per-tab
# ----------------------------------------------------------------------------
_cache = {}  # sheet_title -> {"data": [...], "ts": float}
_client = None
_ws_titles_cache = {"titles": None, "ts": 0}
_tabs_cache = {"tabs": None, "ts": 0}


def get_client():
    global _client
    if _client is None:
        if not SPREADSHEET_ID:
            raise RuntimeError(
                "SPREADSHEET_ID kosong. Pastikan file .env ada di folder ini dan "
                "berisi baris SPREADSHEET_ID=... (bukan cuma .env.example)."
            )
        scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

        if SERVICE_ACCOUNT_JSON:
            # Mode env var (dipakai di platform tanpa upload file rahasia, mis. Vercel)
            try:
                info = json.loads(SERVICE_ACCOUNT_JSON)
            except json.JSONDecodeError as e:
                raise RuntimeError(
                    f"SERVICE_ACCOUNT_JSON bukan JSON valid — pastikan isinya persis "
                    f"seluruh isi file service_account.json, satu baris: {e}"
                )
            try:
                creds = Credentials.from_service_account_info(info, scopes=scopes)
            except Exception as e:
                raise RuntimeError(f"Gagal pakai SERVICE_ACCOUNT_JSON sebagai kredensial: {e}")
        else:
            # Mode file (dipakai di lokal/Render lewat Secret Files)
            if not os.path.exists(SERVICE_ACCOUNT_FILE):
                raise RuntimeError(
                    f"File kredensial '{SERVICE_ACCOUNT_FILE}' tidak ditemukan di folder ini."
                )
            if os.path.getsize(SERVICE_ACCOUNT_FILE) == 0:
                raise RuntimeError(f"File kredensial '{SERVICE_ACCOUNT_FILE}' kosong (0 byte).")
            try:
                creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
            except Exception as e:
                raise RuntimeError(
                    f"Gagal baca '{SERVICE_ACCOUNT_FILE}' sebagai kredensial JSON valid: {e}"
                )

        _client = gspread.authorize(creds)
    return _client


def get_all_worksheets(sh):
    """Daftar semua worksheet di spreadsheet, di-cache singkat (dipakai
    bareng oleh get_worksheet() dan get_active_tabs())."""
    now = time.time()
    if _ws_titles_cache["titles"] is None or (now - _ws_titles_cache["ts"]) > CACHE_SECONDS:
        _ws_titles_cache["titles"] = sh.worksheets()
        _ws_titles_cache["ts"] = now
    return _ws_titles_cache["titles"]


def get_active_tabs(sh):
    """Daftar (label, title) tab yang ditampilkan di dashboard. Kalau
    AUTO_DETECT_WEEKS aktif, hitung ulang dari daftar tab asli di spreadsheet
    tiap kali cache-nya kedaluwarsa — jadi tab minggu baru otomatis muncul."""
    if not AUTO_DETECT_WEEKS:
        return TABS

    now = time.time()
    if _tabs_cache["tabs"] is not None and (now - _tabs_cache["ts"]) < CACHE_SECONDS:
        return _tabs_cache["tabs"]

    all_ws = get_all_worksheets(sh)
    weeks = []
    for ws in all_ws:
        m = WEEK_TAB_REGEX.match(ws.title.strip())
        if m:
            weeks.append((int(m.group(1)), ws.title))
    weeks.sort(key=lambda pair: pair[0])
    tabs = [(f"Week {n}", title) for n, title in weeks]

    existing_lower = {ws.title.strip().lower() for ws in all_ws}
    for label, title in EXTRA_TABS:
        if title.strip().lower() in existing_lower:
            tabs.append((label, title))

    _tabs_cache["tabs"] = tabs
    _tabs_cache["ts"] = now
    return tabs


def get_worksheet(sh, sheet_title):
    """Cari worksheet by title; kalau tidak ketemu persis, coba cocokkan
    longgar (trim + case-insensitive) dulu sebelum menyerah, dan kalau
    tetap gagal, tampilkan daftar tab yang benar-benar ada di spreadsheet
    supaya gampang di-debug."""
    try:
        return sh.worksheet(sheet_title)
    except gspread.exceptions.WorksheetNotFound:
        pass

    all_ws = get_all_worksheets(sh)
    target = sheet_title.strip().lower()
    for ws in all_ws:
        if ws.title.strip().lower() == target:
            return ws

    available = ", ".join(f'"{ws.title}"' for ws in all_ws) or "(tidak ada tab sama sekali)"
    raise gspread.exceptions.WorksheetNotFound(
        f'Tab "{sheet_title}" tidak ditemukan. Tab yang ada di spreadsheet: {available}'
    )


def get_sheet_grid(sh, sheet_title):
    """Ambil nilai tampilan tiap sel SEKALIGUS warna background-nya, dalam
    satu panggilan API (butuh warna background sel FINAL untuk status)."""
    ws = get_worksheet(sh, sheet_title)
    meta = sh.fetch_sheet_metadata(params={
        "ranges": f"'{ws.title}'!A1:AZ3000",
        "includeGridData": "true",
        "fields": "sheets(data(rowData(values(formattedValue,effectiveFormat(backgroundColor)))))",
    })
    sheets_meta = meta.get("sheets", [])
    if not sheets_meta:
        return [], []
    data = sheets_meta[0].get("data", [])
    if not data:
        return [], []
    row_data = data[0].get("rowData", [])

    values_grid = []
    color_grid = []
    for row in row_data:
        cells = row.get("values", [])
        vrow, crow = [], []
        for cell in cells:
            vrow.append(cell.get("formattedValue", "") or "")
            bg = (cell.get("effectiveFormat") or {}).get("backgroundColor")
            crow.append(rgb_of(bg))
        values_grid.append(vrow)
        color_grid.append(crow)
    return values_grid, color_grid


def get_sheet_records(sheet_title):
    """Ambil semua baris sebuah tab (sudah di-parse), dengan cache singkat
    supaya tidak boros kuota Google Sheets API saat banyak user login bersamaan."""
    now = time.time()
    cached = _cache.get(sheet_title)
    if cached is not None and (now - cached["ts"]) < CACHE_SECONDS:
        return cached["data"]

    client = get_client()
    sh = client.open_by_key(SPREADSHEET_ID)
    values, colors = get_sheet_grid(sh, sheet_title)
    records = parse_tab(values, colors)
    _cache[sheet_title] = {"data": records, "ts": now}
    return records


def find_row_for_key(sheet_title, sheet_key):
    records = get_sheet_records(sheet_title)
    target = normalize(sheet_key)
    for row in records:
        if row["norm"] == target:
            return row
    return None


def build_weeks_for_user(sheet_key):
    """Loop semua tab (auto-detect atau manual), cari baris milik user,
    susun jadi list card siap-tampil."""
    client = get_client()
    sh = client.open_by_key(SPREADSHEET_ID)
    tabs = get_active_tabs(sh)

    weeks = []
    for label, title in tabs:
        error = None
        row = None
        try:
            row = find_row_for_key(title, sheet_key)
        except gspread.exceptions.WorksheetNotFound as e:
            error = str(e)
        except Exception as e:
            error = f"Gagal membaca tab ini: {e}"

        card = None
        status = "none"
        if row is not None:
            card = row
            status = card.get("final_status", "belum")

        weeks.append({
            "label": label,
            "card": card,
            "status": status,
            "status_label": STATUS_LABEL.get(status, "-"),
            "error": error,
        })
    return weeks


# ----------------------------------------------------------------------------
# Auth helpers
# ----------------------------------------------------------------------------
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("sheet_key"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return wrapper


def is_locked_out():
    lock_until = session.get("lock_until", 0)
    return time.time() < lock_until


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------
@app.route("/", methods=["GET"])
def index():
    if session.get("sheet_key"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if is_locked_out():
            flash("Terlalu banyak percobaan gagal. Coba lagi beberapa menit lagi.", "error")
            return render_template("login.html")

        pin = request.form.get("pin", "").strip()
        user = find_user_by_pin(pin)

        if user is None:
            session["attempts"] = session.get("attempts", 0) + 1
            if session["attempts"] >= MAX_LOGIN_ATTEMPTS:
                session["lock_until"] = time.time() + LOCKOUT_SECONDS
                flash("Terlalu banyak percobaan gagal. Coba lagi beberapa menit lagi.", "error")
            else:
                flash("PIN tidak dikenali.", "error")
            return render_template("login.html")

        session.clear()
        session["sheet_key"] = user["sheet_key"]
        session["display_name"] = user["display_name"]
        return redirect(url_for("dashboard"))

    return render_template("login.html")


@app.route("/dashboard")
@login_required
def dashboard():
    try:
        weeks = build_weeks_for_user(session["sheet_key"])
        load_error = None
    except Exception as e:
        weeks = []
        load_error = f"Gagal menghubungi Google Sheets: {e}"

    return render_template(
        "dashboard.html",
        display_name=session["display_name"],
        weeks=weeks,
        load_error=load_error,
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


init_db()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
