# Kencana Analytics — Backend

Backend untuk sistem analisis Big Data Kencana Travel: **Segmentasi Pasar**,
**Forecasting Demand**, dan **Analisis Performa Rute**. Dibangun mengacu pada
DFD Level 0/1, ERD, Class Diagram, dan Use Case Diagram yang sudah disusun
sebelumnya, serta menyesuaikan tampilan UI/UX (Login, Dashboard, Data Transaksi,
Segmentasi, Forecasting, Performa Rute) yang sudah dikirim.

## 1. Rancangan Arsitektur

```
Frontend (UI/UX yang sudah ada)
        |
        v  HTTP + JWT
+-------------------------+
|   FastAPI (app/main.py) |
|  - Router per modul     |
|  - Auth (JWT + role)    |
+-------------------------+
        |
        v
+-------------------------+        +----------------------+
|  Service Layer          | -----> | scikit-learn (KMeans) |
|  - segmentasi_service   |        | statsmodels (ARIMA)   |
|  - forecasting_service  |        |                      |
|  - performa_service     |        +----------------------+
+-------------------------+
        |
        v  SQLAlchemy ORM
+-------------------------+
|  Database (SQLite,      |
|  bisa ganti PostgreSQL) |
+-------------------------+
```

Stack: **FastAPI** (REST API) + **SQLAlchemy** (ORM) + **SQLite** (default,
tinggal ganti `DATABASE_URL` env var ke PostgreSQL untuk produksi tanpa ubah kode) +
**scikit-learn** (K-Means) + **statsmodels** (forecasting time-series).

## 2. Skema Database (sesuai ERD/Class Diagram)

`Cabang`, `Rute`, `Armada`, `Jadwal`, `Member`, `JenisPaket`, `Transaksi` (fact table),
`Pengguna` (akun login: Admin/Owner/KepalaOutlet).

## 3. Peta Endpoint ke Halaman UI

| Halaman UI | Endpoint |
|---|---|
| Login | `POST /api/auth/login` |
| Dashboard | `GET /api/dashboard/summary`, `/distribusi-member`, `/aktivitas-terkini` |
| Data Transaksi | `GET /api/transaksi` (filter+pagination), `POST /api/transaksi`, `GET /api/transaksi/export` |
| Segmentasi | `GET /api/segmentasi/ringkasan`, `GET /api/segmentasi/cluster` (RFM+K-Means) |
| Forecasting | `GET /api/forecasting/rute-tersedia`, `POST /api/forecasting/run` (ARIMA) |
| Performa Rute | `GET /api/performa-rute`, `GET /api/performa-rute/vip-vs-reguler` |

Dokumentasi interaktif lengkap (Swagger) otomatis tersedia di `/docs` setelah server jalan.

## 4. Algoritma yang Dipakai (sesuai kesepakatan sebelumnya)

- **Import & Cleaning** (`/api/import`): upload CSV, validasi kolom wajib, buang baris kosong/tidak
  valid, hapus duplikat (di file & yang sudah ada di DB), lalu simpan + catat di riwayat import.
- **EDA** (`/api/eda`): statistik deskriptif, tren bulanan, pola hari, efek akhir pekan/libur
  (tabel `kalender`), distribusi per rute/layanan/channel/member.
- **Segmentasi Pasar**: RFM per pelanggan -> log(1+x) -> Min-Max -> **K-Means** K = 2..8.
  K dipilih dari Silhouette tertinggi (Elbow & DBI ikut dilaporkan); valid bila Silhouette >= 0,5.
- **Forecasting Demand**: per rute dibandingkan **SARIMAX** (musiman 7 hari + dummy kalender:
  akhir pekan, libur nasional, libur sekolah, sekitar libur; ordo dipilih AIC) dan **ARIMA**
  baseline (tanpa musiman, tanpa eksogen). Diuji di 28 hari terakhir; model dengan MAPE terkecil
  dipilih (kategori Lewis; valid bila MAPE <= 20%).
- **Performa Rute & Cabang**: jumlah perjalanan, penumpang, pendapatan, okupansi rata-rata per
  perjalanan, status Tinggi/Sedang/Rendah, filter periode.
- **Laporan** (`/api/laporan`): PDF/Excel (ringkasan, performa, segmentasi, forecasting) dan alur
  permintaan laporan Owner/Kepala Outlet -> diproses Admin.
- **Data Master** (`/api/master`): CRUD rute, armada, jadwal (Kepala Outlet: cabangnya saja).
- Kepala Outlet hanya melihat data cabangnya (`get_cabang_scope`). Analisis berat di-cache di memori
  dan dipanaskan di thread latar saat server start (`PANASKAN_CACHE=0` untuk mematikan).

## 4b. Perbaikan Revisi Capstone (Okt 2026)

- **Analisis Terpadu** (`GET /api/integrasi`): menghubungkan Segmentasi - Forecasting - Performa Rute
  (komposisi segmen per rute, korelasi Pearson/Spearman pada panel rute x bulan, matriks prioritas aksi).
- **Reduksi atribut berbasis algoritma** (`GET /api/eda/reduksi-atribut`): Variance Threshold + Missing Filter,
  korelasi (Pearson / Cramer's V), Mutual Information, PCA.
- **EDA diperdalam** (`GET /api/eda`): skewness/kurtosis, pencilan IQR, matriks korelasi, ADF + ACF, pola hari x jam.
- **Laporan Lengkap** (`jenis=lengkap`) dan **Analisis Terpadu** (`jenis=terpadu`) di `/api/laporan/unduh`.
- **ERD sinkron dengan database**: `python scripts/buat_erd.py` membuat `docs/ERD.md` dari `app/models/models.py`
  (`--cek` untuk memeriksa sinkron).

## 5. Role & Akses (sesuai Use Case Diagram)

- **Admin**: akses penuh — input data, jalankan segmentasi/forecasting, tambah transaksi.
- **Owner** & **KepalaOutlet**: hanya bisa melihat dashboard/laporan (read-only),
  tidak bisa tambah transaksi atau menjalankan model (dibatasi lewat `require_roles()`).

## 6. Cara Menjalankan

```bash
# 1. Install dependency
pip install -r requirements.txt

# 2. Dataset sudah ada di data/kencana_transaksi_gabungan.csv

# 3. Migrasi + muat data + akun awal. Data dimuat ulang otomatis bila file CSV berganti.
python -m app.bootstrap

# 4. Jalankan server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Buka `http://localhost:8000/docs` untuk mencoba semua endpoint langsung dari browser.

### Akun default (dari `seed_users.py`)
| Role | Email | Password |
|---|---|---|
| Admin | admin@kencanatravel.co.id | admin123 |
| Owner | owner@kencanatravel.co.id | owner123 |
| KepalaOutlet | kepala.solo@kencanatravel.co.id | kepala123 |

**PENTING**: ganti `JWT_SECRET` (env var) dan password default sebelum dipakai di lingkungan nyata.

## 7. Catatan & Keterbatasan

1. **Dataset adalah data sintetis yang dikalibrasi dari laporan bulanan outlet Kencana Travel**
   (Januari-Agustus 2026; outlet Solo, Semarang, Tayu), dibuat oleh `scripts/buat_data_sintetis.py`.
   Laporan asli hanya berupa rekap bulanan per outlet (tanpa transaksi, pelanggan, atau data harian),
   jadi transaksi per pemesanan dibangkitkan dengan volume penumpang, trip, kapasitas, tarif rata-rata,
   porsi diskon, dan pola bulanan yang mengikuti laporan tersebut. Periode 1 Sep 2023 - 31 Agu 2026,
   257.228 transaksi, ~14 ribu pelanggan, tanpa transaksi paket (laporan asli: paket = 0).
2. **Nama pelanggan dipakai sebagai ID pelanggan** untuk RFM. Bila data transaksi asli Kencana
   (dengan ID/nomor HP pelanggan) tersedia, cukup import CSV dengan kolom "Nama Pelanggan" berisi ID
   tersebut — logic RFM + K-Means tidak perlu diubah.
3. **Cache analisis di memori proses**: hasil berat (forecasting, segmentasi, performa) disimpan per
   proses dan dihitung ulang setelah restart (dipanaskan otomatis di thread latar).
