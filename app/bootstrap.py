"""Persiapan database sebelum server dijalankan (dipakai sebagai langkah awal start command
di Railway). Aman dijalankan berulang kali:

1. Migrasi skema (buat tabel / tambah kolom yang belum ada).
2. Muat data dari CSV bila:
   - transaksi masih kosong atau belum lengkap (mis. pemuatan sebelumnya terputus), ATAU
   - file CSV dataset di repo sudah berganti (sidik jari SHA-256 berbeda dengan yang tercatat).
   Data referensi & transaksi dihapus dulu lalu dimuat ulang dari awal. Akun pengguna tidak
   disentuh; cabang milik Kepala Outlet & permintaan laporan dipulihkan berdasarkan nama cabang.
3. Bila belum ada akun sama sekali: buat akun awal (lihat app.seed_users).

Jalankan: python -m app.bootstrap"""
import hashlib
import os
import sys
import time
from pathlib import Path
from sqlalchemy import func, text
from app.core.database import SessionLocal
from app.core.migrate import isi_referensi_dari_transaksi, jalankan_migrasi
from app.models.models import (Armada, Cabang, InfoSistem, Jadwal, JenisPaket, Member, Pengguna,
                               PermintaanLaporan, Rute, Transaksi)

CSV_DEFAULT = Path(__file__).resolve().parent.parent / "data" / "kencana_transaksi_gabungan.csv"
KUNCI_HASH = "hash_dataset_csv"


def _jumlah_baris_csv(path: Path) -> int:
    with open(path, "rb") as f:
        return sum(1 for _ in f) - 1  # tanpa header


def _hash_csv(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blok in iter(lambda: f.read(1 << 20), b""):
            h.update(blok)
    return h.hexdigest()


def _baca_info(kunci: str) -> str | None:
    db = SessionLocal()
    try:
        info = db.get(InfoSistem, kunci)
        return info.nilai if info else None
    finally:
        db.close()


def _tulis_info(kunci: str, nilai: str) -> None:
    db = SessionLocal()
    try:
        db.merge(InfoSistem(kunci=kunci, nilai=nilai))
        db.commit()
    finally:
        db.close()


def _hapus_data_referensi() -> tuple[dict, dict]:
    """Kosongkan tabel data (urutan mengikuti foreign key). Akun pengguna dipertahankan.
    Relasi ke cabang dilepas (tabel cabang ikut dibuat ulang); nama cabangnya dikembalikan
    agar bisa dipulihkan setelah data dimuat ulang."""
    db = SessionLocal()
    try:
        cabang_pengguna = {p.id_pengguna: c.nama_cabang for p, c in
                           db.query(Pengguna, Cabang).join(Cabang, Pengguna.id_cabang == Cabang.id_cabang)}
        cabang_permintaan = {p.id_permintaan: p.cabang for p in
                             db.query(PermintaanLaporan).filter(PermintaanLaporan.cabang.isnot(None))}
        db.query(Pengguna).update({Pengguna.id_cabang: None}, synchronize_session=False)
        db.query(PermintaanLaporan).update({PermintaanLaporan.cabang: None}, synchronize_session=False)
        if db.bind.dialect.name == "postgresql":
            # DELETE di Postgres hanya menandai baris sebagai mati — ruang disk tidak kembali
            # sampai VACUUM. TRUNCATE langsung membebaskan ruangnya. (Tabel transaksi tidak
            # direferensikan tabel lain, jadi aman di-TRUNCATE sendiri.)
            db.execute(text("TRUNCATE TABLE transaksi"))
        for model in (Transaksi, Jadwal, Armada, Rute, Member, JenisPaket, Cabang):
            db.query(model).delete(synchronize_session=False)
        db.commit()
        return cabang_pengguna, cabang_permintaan
    finally:
        db.close()


def _pulihkan_cabang(cabang_pengguna: dict, cabang_permintaan: dict) -> None:
    db = SessionLocal()
    try:
        id_per_nama = {c.nama_cabang: c.id_cabang for c in db.query(Cabang)}
        for id_pengguna, nama in cabang_pengguna.items():
            if nama in id_per_nama:
                db.query(Pengguna).filter(Pengguna.id_pengguna == id_pengguna) \
                  .update({Pengguna.id_cabang: id_per_nama[nama]}, synchronize_session=False)
        for id_permintaan, nama in cabang_permintaan.items():
            if nama in id_per_nama:
                db.query(PermintaanLaporan).filter(PermintaanLaporan.id_permintaan == id_permintaan) \
                  .update({PermintaanLaporan.cabang: nama}, synchronize_session=False)
        db.commit()
    finally:
        db.close()


def main() -> None:
    print("[bootstrap] Migrasi skema...", flush=True)
    jalankan_migrasi()

    csv_path = Path(os.getenv("DATA_CSV_PATH", CSV_DEFAULT))
    if not csv_path.exists():
        sys.exit(f"[bootstrap] CSV tidak ditemukan: {csv_path}")
    target = _jumlah_baris_csv(csv_path)
    hash_csv = _hash_csv(csv_path)
    hash_tercatat = _baca_info(KUNCI_HASH)

    db = SessionLocal()
    try:
        jumlah = db.query(func.count(Transaksi.id_transaksi)).scalar() or 0
    finally:
        db.close()

    if hash_tercatat is None and jumlah == target:
        # Database lama sebelum sidik jari dicatat, isinya sama dengan CSV sekarang.
        dataset_berganti = False
    else:
        dataset_berganti = jumlah > 0 and hash_tercatat != hash_csv

    if not dataset_berganti and jumlah >= target:
        print(f"[bootstrap] Data transaksi lengkap ({jumlah:,} baris), lewati pemuatan CSV.", flush=True)
    else:
        simpanan = ({}, {})
        if jumlah > 0:
            alasan = ("file dataset CSV berganti" if dataset_berganti else
                      f"data TIDAK lengkap ({jumlah:,} dari {target:,} baris), kemungkinan pemuatan "
                      "sebelumnya terputus")
            print(f"[bootstrap] {alasan.capitalize()}. Menghapus dan memuat ulang...", flush=True)
            simpanan = _hapus_data_referensi()
        print(f"[bootstrap] Memuat {target:,} baris dari {csv_path.name} (beberapa menit)...", flush=True)
        mulai = time.time()
        from app.load_data import main as load_data
        load_data(str(csv_path), reset=False)
        isi_referensi_dari_transaksi()
        _pulihkan_cabang(*simpanan)
        print(f"[bootstrap] Data dimuat dalam {time.time() - mulai:.0f} detik.", flush=True)
    _tulis_info(KUNCI_HASH, hash_csv)

    from app.seed_users import seed
    seed()
    print("[bootstrap] Selesai.", flush=True)


if __name__ == "__main__":
    main()
