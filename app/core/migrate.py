"""Migrasi ringan saat startup (proyek ini belum memakai Alembic).

- Membuat tabel baru yang belum ada (mis. token_pengguna).
- Menambah kolom baru di tabel pengguna pada database lama. Akun lama otomatis
  dianggap sudah terverifikasi & aktif lewat server_default kolomnya.
- Mengisi tabel kalender & jadwal dari transaksi yang sudah ada, lalu (khusus PostgreSQL)
  menambahkan foreign key transaksi -> kalender, transaksi -> jadwal, rute -> cabang tujuan.
  Foreign key hanya divalidasi (dibaca), tabel transaksi tidak ditulis ulang, jadi tidak
  menambah pemakaian disk.
Aman dijalankan berulang kali."""
import hashlib
import logging
from datetime import timedelta
from sqlalchemy import func, inspect, select, text
from app.core.database import Base, SessionLocal, engine
from app.models import models  # noqa: F401  (registrasi semua model ke Base.metadata)
from app.models.models import Transaksi
from app.services.referensi_service import pastikan_jadwal, pastikan_kalender

log = logging.getLogger(__name__)

KOLOM_PENGGUNA_BARU = {
    "email_terverifikasi": "BOOLEAN NOT NULL DEFAULT '1'",
    "status": "VARCHAR NOT NULL DEFAULT 'aktif'",
    "dibuat_pada": "TIMESTAMP",
}

# Index kolom ber-kardinalitas rendah yang dihapus dari model Transaksi (lihat komentar di sana).
INDEX_DIHAPUS = [
    "ix_transaksi_cabang_asal",
    "ix_transaksi_layanan",
    "ix_transaksi_jenis_transaksi",
    "ix_transaksi_channel_pemesanan",
]


# Kalender diisi sampai setahun lebih setelah transaksi terakhir supaya forecasting punya
# variabel kalender untuk periode yang diramal.
KALENDER_KE_DEPAN_HARI = 400

FK_POSTGRES = {
    "transaksi": [
        ("fk_transaksi_kalender", "(tanggal) REFERENCES kalender (tanggal)"),
        ("fk_transaksi_jadwal",
         "(id_rute, jam_keberangkatan, layanan) REFERENCES jadwal (id_rute, jam_keberangkatan, layanan)"),
    ],
    "rute": [("fk_rute_cabang_tujuan", "(cabang_tujuan) REFERENCES cabang (nama_cabang)")],
}


def isi_referensi_dari_transaksi():
    """Lengkapi kalender & jadwal untuk transaksi yang sudah ada."""
    db = SessionLocal()
    try:
        mulai, akhir = db.execute(select(func.min(Transaksi.tanggal), func.max(Transaksi.tanggal))).one()
        if mulai is None:
            return
        n_kal = pastikan_kalender(db, mulai, akhir + timedelta(days=KALENDER_KE_DEPAN_HARI))
        kombinasi = db.execute(select(Transaksi.id_rute, Transaksi.jam_keberangkatan,
                                      Transaksi.layanan).distinct()).tuples()
        n_jad = pastikan_jadwal(db, kombinasi)
        db.commit()
        if n_kal or n_jad:
            log.info("Migrasi: %d tanggal kalender & %d jadwal ditambahkan", n_kal, n_jad)
    finally:
        db.close()


def _tambah_foreign_key_postgres():
    if engine.dialect.name != "postgresql":
        return  # SQLite tidak mendukung ADD CONSTRAINT; database baru sudah dibuat lengkap.
    insp = inspect(engine)
    with engine.begin() as conn:
        for tabel, daftar in FK_POSTGRES.items():
            ada = {fk["name"] for fk in insp.get_foreign_keys(tabel)}
            for nama, definisi in daftar:
                if nama not in ada and not (tabel == "rute" and _fk_kolom_ada(insp, "rute", "cabang_tujuan")):
                    conn.execute(text(f"ALTER TABLE {tabel} ADD CONSTRAINT {nama} FOREIGN KEY {definisi}"))
                    log.info("Migrasi: foreign key %s ditambahkan", nama)


def _fk_kolom_ada(insp, tabel, kolom):
    return any(fk["constrained_columns"] == [kolom] for fk in insp.get_foreign_keys(tabel))


def jalankan_migrasi():
    Base.metadata.create_all(bind=engine)

    insp = inspect(engine)
    kolom_pengguna = {c["name"] for c in insp.get_columns("pengguna")}
    kolom_transaksi = {c["name"] for c in insp.get_columns("transaksi")}
    index_ada = {i["name"] for i in insp.get_indexes("transaksi")}

    with engine.begin() as conn:
        # Migrasi tabel pengguna
        for nama, definisi in KOLOM_PENGGUNA_BARU.items():
            if nama not in kolom_pengguna:
                conn.execute(text(f"ALTER TABLE pengguna ADD COLUMN {nama} {definisi}"))
                log.info("Migrasi: kolom pengguna.%s ditambahkan", nama)

        # Migrasi tabel transaksi (anonimisasi nama pelanggan)
        if "id_pelanggan" not in kolom_transaksi:
            conn.execute(text("ALTER TABLE transaksi ADD COLUMN id_pelanggan VARCHAR"))
            log.info("Migrasi: kolom transaksi.id_pelanggan ditambahkan")
            if "nama_pelanggan" in kolom_transaksi:
                # Pertahankan identitas pelanggan untuk RFM: ubah nama lama menjadi ID anonim
                # (hash deterministik, tidak bisa dibalik), baru kosongkan nama (kebijakan baru).
                nama_lama = [r[0] for r in conn.execute(text(
                    "SELECT DISTINCT nama_pelanggan FROM transaksi "
                    "WHERE nama_pelanggan IS NOT NULL AND nama_pelanggan <> ''"))]
                if nama_lama:
                    conn.execute(
                        text("UPDATE transaksi SET id_pelanggan = :idp WHERE nama_pelanggan = :nama"),
                        [{"nama": n, "idp": "PLG-" + hashlib.sha256(n.encode("utf-8")).hexdigest()[:10].upper()}
                         for n in nama_lama])
                    log.info("Migrasi: %d pelanggan dianonimkan ke id_pelanggan", len(nama_lama))
                conn.execute(text("UPDATE transaksi SET nama_pelanggan = NULL"))
                log.info("Migrasi: kolom nama_pelanggan dikosongkan")

        for nama in INDEX_DIHAPUS:
            if nama in index_ada:
                conn.execute(text(f"DROP INDEX {nama}"))
                log.info("Migrasi: index %s dihapus", nama)
        # Tabel jadwal lama belum punya unique constraint yang dibutuhkan foreign key gabungan.
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_jadwal_rute_jam_layanan "
                          "ON jadwal (id_rute, jam_keberangkatan, layanan)"))

    isi_referensi_dari_transaksi()
    _tambah_foreign_key_postgres()
