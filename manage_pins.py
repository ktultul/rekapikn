"""
CLI untuk mengelola PIN login per orang.

Contoh pemakaian:
    python manage_pins.py add "Maiden" "Maiden"
        -> tambah user dengan nama tampilan "Maiden", dicocokkan ke sheet
           lewat nilai "Maiden" (harus persis sama dengan isi kolom
           "Nama - Nick (Discord IKN)" atau "Nama - Nick (Discord Bangsa)"
           di sheet FINAL Rekap), PIN dibuat otomatis (acak).

    python manage_pins.py add "Maiden" "Maiden" --pin 583920
        -> sama seperti di atas tapi PIN ditentukan manual.

    python manage_pins.py list
        -> tampilkan semua user & PIN-nya.

    python manage_pins.py remove "Maiden"
        -> hapus user berdasarkan nama tampilan.
"""
import argparse
import json
import secrets
import sqlite3
import sys

DB_PATH = "users.db"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
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
    return conn


def generate_pin(conn, length=6):
    while True:
        pin = "".join(secrets.choice("0123456789") for _ in range(length))
        exists = conn.execute("SELECT 1 FROM users WHERE pin = ?", (pin,)).fetchone()
        if not exists:
            return pin


def cmd_add(args):
    conn = get_db()
    pin = args.pin or generate_pin(conn)
    existing = conn.execute("SELECT 1 FROM users WHERE pin = ?", (pin,)).fetchone()
    if existing:
        print(f"PIN {pin} sudah dipakai. Pilih PIN lain atau kosongkan --pin untuk auto-generate.")
        sys.exit(1)

    conn.execute(
        "INSERT INTO users (pin, sheet_key, display_name) VALUES (?, ?, ?)",
        (pin, args.sheet_key, args.display_name),
    )
    conn.commit()
    print(f"Berhasil ditambahkan: {args.display_name}  (cocok ke sheet: '{args.sheet_key}')  PIN: {pin}")


def cmd_list(args):
    conn = get_db()
    rows = conn.execute("SELECT * FROM users ORDER BY display_name").fetchall()
    if not rows:
        print("Belum ada user terdaftar.")
        return
    print(f"{'Nama':<25} {'Sheet Key':<25} {'PIN':<10}")
    print("-" * 60)
    for r in rows:
        print(f"{r['display_name']:<25} {r['sheet_key']:<25} {r['pin']:<10}")


def cmd_remove(args):
    conn = get_db()
    cur = conn.execute("DELETE FROM users WHERE display_name = ?", (args.display_name,))
    conn.commit()
    if cur.rowcount:
        print(f"Berhasil dihapus: {args.display_name}")
    else:
        print(f"Tidak ditemukan user dengan nama: {args.display_name}")


def cmd_export_json(args):
    """Cetak seluruh isi users.db sebagai satu baris JSON, siap ditempel ke
    environment variable PINS_JSON di platform tanpa disk permanen (Vercel).
    users.db kamu TIDAK ikut ke-deploy dalam mode ini — cukup env var ini."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM users").fetchall()
    data = {r["pin"]: {"sheet_key": r["sheet_key"], "display_name": r["display_name"]} for r in rows}
    print(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    print(f"\n({len(data)} user — copy baris JSON di atas ke env var PINS_JSON)", file=sys.stderr)


def cmd_bulk(args):
    """Baca file teks berisi banyak nama sekaligus, lalu buat PIN untuk semuanya.

    Format tiap baris di file:
        Nama Tampilan | Nama Persis Di Sheet
    Kalau nama tampilan sama persis dengan nama di sheet, cukup tulis satu:
        Maiden
    Baris kosong atau diawali # akan dilewati.
    """
    conn = get_db()
    hasil = []
    with open(args.file, "r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "|" in line:
                display_name, sheet_key = [p.strip() for p in line.split("|", 1)]
            else:
                display_name = sheet_key = line

            existing = conn.execute(
                "SELECT pin FROM users WHERE display_name = ?", (display_name,)
            ).fetchone()
            if existing:
                hasil.append((display_name, sheet_key, existing["pin"], "sudah ada (dilewati)"))
                continue

            pin = generate_pin(conn)
            conn.execute(
                "INSERT INTO users (pin, sheet_key, display_name) VALUES (?, ?, ?)",
                (pin, sheet_key, display_name),
            )
            conn.commit()
            hasil.append((display_name, sheet_key, pin, "baru dibuat"))

    print(f"{'Nama':<25} {'Sheet Key':<25} {'PIN':<10} {'Status'}")
    print("-" * 75)
    for display_name, sheet_key, pin, status in hasil:
        print(f"{display_name:<25} {sheet_key:<25} {pin:<10} {status}")
    print(f"\nTotal diproses: {len(hasil)}")


def main():
    parser = argparse.ArgumentParser(description="Kelola PIN login user rekap")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="Tambah user baru")
    p_add.add_argument("display_name", help="Nama yang ditampilkan setelah login")
    p_add.add_argument(
        "sheet_key",
        help="Nilai yang harus sama persis dengan isi kolom nama di sheet FINAL Rekap",
    )
    p_add.add_argument("--pin", help="PIN manual (opsional, default: acak 6 digit)")
    p_add.set_defaults(func=cmd_add)

    p_list = sub.add_parser("list", help="Tampilkan semua user")
    p_list.set_defaults(func=cmd_list)

    p_remove = sub.add_parser("remove", help="Hapus user")
    p_remove.add_argument("display_name")
    p_remove.set_defaults(func=cmd_remove)

    p_bulk = sub.add_parser("bulk", help="Tambah banyak user sekaligus dari file teks")
    p_bulk.add_argument("file", help="Path ke file teks daftar nama (lihat contoh: nama_list.txt)")
    p_bulk.set_defaults(func=cmd_bulk)

    p_export = sub.add_parser("export-json", help="Cetak semua PIN sebagai JSON untuk env var PINS_JSON (Vercel dsb)")
    p_export.set_defaults(func=cmd_export_json)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
