# Cek Rekap Pribadi — BEYOND

Web app: tiap member login pakai PIN unik dan hanya melihat baris data
miliknya sendiri, dengan tampilan slide per minggu (Week 1–6 + Yang Bakal
Cair).

## ⚠️ Sebelum mulai — rotasi kredensial

Kalau kamu pernah share `service_account.json` atau isi `pin_list.txt` ke
tempat yang tidak sepenuhnya privat (termasuk chat AI), anggap keduanya
bocor:

1. Di [Google Cloud Console](https://console.cloud.google.com/iam-admin/serviceaccounts),
   hapus key lama service account itu dan buat key baru.
2. Regenerasi semua PIN dengan `python manage_pins.py bulk nama_list.txt`
   setelah hapus dulu isi lama di `users.db` (atau hapus file `users.db`,
   nanti otomatis dibuat ulang saat `app.py` pertama jalan).
3. Bagikan PIN baru ke member secara pribadi (DM), jangan di channel umum.

## Cara kerja singkat

- Data dibaca **langsung dari Google Sheet** (live) lewat `service_account.json`,
  untuk setiap tab minggu yang dikonfigurasi di `SHEET_TABS` (lihat `.env.example`).
- PIN disimpan **terpisah** di `users.db` (SQLite) — bukan di Google Sheet.
- Saat login, app mencocokkan PIN → nama → baris yang cocok di kolom
  "Nama - Nick (Discord IKN)" / "Nama - Nick (Discord Bangsa)" di **setiap
  tab**, lalu tampilkan semuanya dalam mode slide (geser kiri/kanan atau
  swipe di HP).
- Tiap slide menampilkan PB Elan, PB CM, PB Markas, Admin, Total, Final,
  Rekap Sebelumnya Yang Belum Cair (kalau ada), dan Alasan Tidak Gajian
  (kalau ada) — plus badge status Cair / Belum Cair / Digabung otomatis
  dari isi kolom FINAL.

## 1. Setup awal

```bash
cd rekap-app
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# lalu edit .env: isi SECRET_KEY dengan string acak, sesuaikan SHEET_TABS kalau perlu
```

Taruh `service_account.json` (yang **baru**, hasil rotasi) di folder ini.
Akun servis itu harus punya akses **Viewer** ke spreadsheet (share email
service account-nya ke sheet kalau belum, atau pastikan sheet-nya publik).

## 2. Kelola user & PIN

```bash
python manage_pins.py bulk nama_list.txt      # generate PIN untuk semua nama di file
python manage_pins.py list                     # lihat semua user & PIN
python manage_pins.py add "Nama Baru" "Kunci Di Sheet"   # tambah satu-satu
python manage_pins.py remove "Nama"             # hapus user
```

`nama_list.txt` berisi daftar nama (satu per baris) yang harus **persis
sama** dengan isi kolom nama di sheet. Bagikan PIN hasil generate ke
masing-masing orang secara pribadi.

## 3. Coba jalankan lokal

```bash
python app.py
```
Buka `http://localhost:5000`, login pakai salah satu PIN dari langkah 2.

## 4. Deploy gratis (rekomendasi: Render.com)

1. Push folder ini ke repo GitHub (`.env` dan `service_account.json` sudah
   ada di `.gitignore` — pastikan tidak ikut ter-commit).
2. Buka [render.com](https://render.com) → New → Web Service → hubungkan
   repo GitHub kamu.
3. Isi:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app`
4. Di tab **Environment**, tambahkan semua variabel dari `.env.example`.
5. Untuk `service_account.json` dan `users.db` (rahasia, tidak boleh ikut
   ke GitHub):
   - Pakai fitur **Secret Files** di Render untuk upload `service_account.json`.
   - `users.db` otomatis dibuat ulang saat pertama app jalan (tabel kosong).
     Supaya isinya tidak hilang tiap re-deploy, pakai **Render Disk**
     (persistent disk kecil, tersedia di paket gratis dengan batas
     terbatas) dan set `DB_PATH` ke path di disk itu. Setelah disk aktif,
     jalankan ulang `python manage_pins.py bulk nama_list.txt` sekali di
     shell Render untuk isi datanya.
6. Deploy. Render kasih URL publik (misal `https://rekap-beyond.onrender.com`)
   — itu yang dibagikan ke member.

Catatan: paket gratis Render "tidur" kalau tidak ada yang akses beberapa
menit, jadi request pertama setelah lama nganggur agak lambat (~30 detik).
Normal untuk paket gratis.

## 4b. Deploy alternatif: Vercel

Vercel **tidak punya disk permanen** dan **tidak bisa upload file rahasia**
seperti Render — jadi dua hal itu perlu lewat environment variable, bukan
file. App ini sudah mendukung mode ini otomatis (baca dari env var kalau
ada, kalau tidak baru fallback ke file/sqlite seperti biasa).

1. Push folder ini ke repo GitHub (`service_account.json` dan `users.db`
   memang tidak akan ikut — sesuai `.gitignore`, itu sengaja).
2. Di [vercel.com](https://vercel.com), New Project → import repo ini.
   **Pastikan Framework Preset di-set ke "Other"**, BUKAN Next.js — ini
   penyebab paling umum error saat import.
3. Generate isi `PINS_JSON` dari PIN yang sudah ada:
   ```bash
   python manage_pins.py export-json
   ```
   Copy baris JSON yang dicetak (bukan baris keterangan jumlah user di
   bawahnya, itu cuma info).
4. Di Vercel → Project Settings → **Environment Variables**, tambahkan:
   - `SPREADSHEET_ID` — sama seperti di `.env`
   - `SECRET_KEY` — string acak baru, jangan pakai yang sama dengan Render
   - `SERVICE_ACCOUNT_JSON` — **seluruh isi** file `service_account.json`
     kamu, paste apa adanya (satu baris utuh)
   - `PINS_JSON` — hasil `manage_pins.py export-json` di langkah 3
   - `AUTO_DETECT_WEEKS`, `SHEET_EXTRA_TABS`, `CACHE_SECONDS` — opsional,
     sama seperti `.env.example`
5. Deploy. Vercel kasih URL publik `https://nama-app.vercel.app`.

**Penting:** `PINS_JSON` itu snapshot, bukan live seperti sqlite. Tambah
PIN baru lewat `manage_pins.py` di komputer sendiri → jalankan
`export-json` lagi → update env var-nya manual di dashboard Vercel →
redeploy. Tidak otomatis sinkron seperti di Render.

## Tab minggu baru — otomatis, tidak perlu apa-apa

Default-nya (`AUTO_DETECT_WEEKS=true` di `.env`), app **otomatis mendeteksi**
semua tab yang cocok pola nama `BEYOND (Week <angka>)` setiap kali cache-nya
kedaluwarsa (default 60 detik, atur lewat `CACHE_SECONDS`). Jadi begitu kamu
buat tab baru di Google Sheets (`BEYOND (Week 7)`, `Week 8`, dst — via klik
kanan "Duplicate" tab lama seperti biasa), dashboard **langsung ikut
menampilkannya** tanpa perlu edit `.env`, edit kode, atau restart app.

Urutannya otomatis diurutkan dari angka minggu terkecil ke terbesar, dan tab
"Yang Bakal Cair" selalu ditaruh paling akhir (diatur lewat `SHEET_EXTRA_TABS`
di `.env`, defaultnya sudah pas).

**Kalau suatu saat butuh kontrol manual** (misal nama tab minggu tidak
mengikuti pola itu lagi, atau mau urutan/label custom): isi `SHEET_TABS` di
`.env` dengan daftar eksplisit — begitu diisi, auto-detect otomatis mati dan
app pakai persis daftar itu:

```
SHEET_TABS=Week 1|BEYOND (Week 1);Week 2|BEYOND (Week 2);Yang Bakal Cair|Yang Bakal Cair
```

## Keamanan & catatan tambahan

- PIN disimpan apa adanya (plain) di `users.db` untuk kesederhanaan. Kalau
  mau lebih aman, bisa di-hash — bilang aja kalau perlu di-upgrade.
- 5x salah PIN berturut-turut mengunci percobaan login selama 5 menit.
- Data tiap tab di-cache 60 detik di server (bisa diubah lewat
  `CACHE_SECONDS`) supaya tidak boros kuota Google Sheets API.
- Kalau ada kolom baru ditambahkan di sheet, sesuaikan `CARD_FIELD_MAP`
  di `app.py` kalau mau ikut ditampilkan.
