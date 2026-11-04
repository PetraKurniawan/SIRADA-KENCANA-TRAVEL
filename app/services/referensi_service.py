"""
Data referensi yang diturunkan dari transaksi: Kalender (dimensi tanggal) dan Jadwal.

Dipanggil oleh load_data (pemuatan awal), migrasi (database lama), import CSV, dan tambah
transaksi, supaya foreign key transaksi -> kalender dan transaksi -> jadwal selalu terpenuhi.
"""
from datetime import date, timedelta
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.models import Jadwal, Kalender

NAMA_HARI = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]

# Libur nasional & cuti bersama (SKB 3 Menteri). Tanggal 2027 masih perkiraan karena SKB-nya
# belum terbit; hari raya berbasis kalender hijriah/lunar bisa bergeser 1 hari.
LIBUR_NASIONAL = {
    # 2023
    "2023-09-28": "Maulid Nabi Muhammad SAW",
    "2023-12-25": "Hari Raya Natal", "2023-12-26": "Cuti bersama Natal",
    # 2024
    "2024-01-01": "Tahun Baru Masehi", "2024-02-08": "Isra Mikraj",
    "2024-02-09": "Cuti bersama Imlek", "2024-02-10": "Tahun Baru Imlek",
    "2024-03-11": "Hari Suci Nyepi", "2024-03-12": "Cuti bersama Nyepi",
    "2024-03-29": "Wafat Isa Almasih", "2024-03-31": "Hari Paskah",
    "2024-04-08": "Cuti bersama Idul Fitri", "2024-04-09": "Cuti bersama Idul Fitri",
    "2024-04-10": "Idul Fitri", "2024-04-11": "Idul Fitri",
    "2024-04-12": "Cuti bersama Idul Fitri", "2024-04-15": "Cuti bersama Idul Fitri",
    "2024-05-01": "Hari Buruh", "2024-05-09": "Kenaikan Isa Almasih",
    "2024-05-10": "Cuti bersama Kenaikan", "2024-05-23": "Hari Raya Waisak",
    "2024-05-24": "Cuti bersama Waisak", "2024-06-01": "Hari Lahir Pancasila",
    "2024-06-17": "Idul Adha", "2024-06-18": "Cuti bersama Idul Adha",
    "2024-07-07": "Tahun Baru Islam", "2024-08-17": "Hari Kemerdekaan RI",
    "2024-09-16": "Maulid Nabi Muhammad SAW",
    "2024-12-25": "Hari Raya Natal", "2024-12-26": "Cuti bersama Natal",
    # 2025
    "2025-01-01": "Tahun Baru Masehi", "2025-01-27": "Isra Mikraj",
    "2025-01-28": "Cuti bersama Imlek", "2025-01-29": "Tahun Baru Imlek",
    "2025-03-28": "Cuti bersama Nyepi", "2025-03-29": "Hari Suci Nyepi",
    "2025-03-31": "Idul Fitri", "2025-04-01": "Idul Fitri",
    "2025-04-02": "Cuti bersama Idul Fitri", "2025-04-03": "Cuti bersama Idul Fitri",
    "2025-04-04": "Cuti bersama Idul Fitri", "2025-04-07": "Cuti bersama Idul Fitri",
    "2025-04-18": "Wafat Isa Almasih", "2025-04-20": "Hari Paskah",
    "2025-05-01": "Hari Buruh", "2025-05-12": "Hari Raya Waisak",
    "2025-05-13": "Cuti bersama Waisak", "2025-05-29": "Kenaikan Isa Almasih",
    "2025-05-30": "Cuti bersama Kenaikan", "2025-06-01": "Hari Lahir Pancasila",
    "2025-06-06": "Idul Adha", "2025-06-09": "Cuti bersama Idul Adha",
    "2025-06-27": "Tahun Baru Islam", "2025-08-17": "Hari Kemerdekaan RI",
    "2025-08-18": "Cuti bersama Kemerdekaan", "2025-09-05": "Maulid Nabi Muhammad SAW",
    "2025-12-25": "Hari Raya Natal", "2025-12-26": "Cuti bersama Natal",
    # 2026
    "2026-01-01": "Tahun Baru Masehi", "2026-01-16": "Isra Mikraj",
    "2026-02-16": "Cuti bersama Imlek", "2026-02-17": "Tahun Baru Imlek",
    "2026-03-18": "Cuti bersama Nyepi", "2026-03-19": "Hari Suci Nyepi",
    "2026-03-20": "Idul Fitri", "2026-03-21": "Idul Fitri",
    "2026-03-23": "Cuti bersama Idul Fitri", "2026-03-24": "Cuti bersama Idul Fitri",
    "2026-04-03": "Wafat Isa Almasih", "2026-04-05": "Hari Paskah",
    "2026-05-01": "Hari Buruh", "2026-05-14": "Kenaikan Isa Almasih",
    "2026-05-15": "Cuti bersama Kenaikan", "2026-05-27": "Idul Adha",
    "2026-05-28": "Cuti bersama Idul Adha", "2026-05-31": "Hari Raya Waisak",
    "2026-06-01": "Hari Lahir Pancasila", "2026-06-16": "Tahun Baru Islam",
    "2026-08-17": "Hari Kemerdekaan RI", "2026-08-25": "Maulid Nabi Muhammad SAW",
    "2026-12-24": "Cuti bersama Natal", "2026-12-25": "Hari Raya Natal",
    # 2027 (perkiraan)
    "2027-01-01": "Tahun Baru Masehi", "2027-01-05": "Isra Mikraj",
    "2027-02-06": "Tahun Baru Imlek", "2027-03-08": "Hari Suci Nyepi",
    "2027-03-10": "Idul Fitri", "2027-03-11": "Idul Fitri",
    "2027-03-26": "Wafat Isa Almasih", "2027-03-28": "Hari Paskah",
    "2027-05-01": "Hari Buruh", "2027-05-06": "Kenaikan Isa Almasih",
    "2027-05-17": "Idul Adha", "2027-05-20": "Hari Raya Waisak",
    "2027-06-01": "Hari Lahir Pancasila", "2027-06-06": "Tahun Baru Islam",
    "2027-08-15": "Maulid Nabi Muhammad SAW", "2027-08-17": "Hari Kemerdekaan RI",
    "2027-12-25": "Hari Raya Natal",
}

# Libur sekolah (perkiraan kalender pendidikan Jawa Tengah): semester ganjil, kenaikan kelas,
# dan libur sekitar Idul Fitri.
LIBUR_SEKOLAH = [
    ("2023-12-23", "2024-01-06"), ("2024-04-04", "2024-04-17"), ("2024-06-22", "2024-07-13"),
    ("2024-12-21", "2025-01-04"), ("2025-03-24", "2025-04-08"), ("2025-06-28", "2025-07-12"),
    ("2025-12-20", "2026-01-03"), ("2026-03-14", "2026-03-28"), ("2026-06-27", "2026-07-11"),
    ("2026-12-19", "2027-01-02"), ("2027-03-04", "2027-03-17"), ("2027-06-26", "2027-07-10"),
]


def _rentang_libur_sekolah() -> set[date]:
    hasil = set()
    for mulai, selesai in LIBUR_SEKOLAH:
        d, akhir = date.fromisoformat(mulai), date.fromisoformat(selesai)
        while d <= akhir:
            hasil.add(d)
            d += timedelta(days=1)
    return hasil


def baris_kalender(mulai: date, selesai: date) -> list[dict]:
    sekolah = _rentang_libur_sekolah()
    hasil = []
    d = mulai
    while d <= selesai:
        ket = LIBUR_NASIONAL.get(d.isoformat())
        hasil.append({
            "tanggal": d,
            "hari": NAMA_HARI[d.weekday()],
            "weekend": d.weekday() >= 5,
            "libur_nasional": ket is not None,
            "libur_sekolah": d in sekolah,
            "keterangan": ket,
        })
        d += timedelta(days=1)
    return hasil


def pastikan_kalender(db: Session, mulai: date, selesai: date) -> int:
    """Tambahkan tanggal yang belum ada di tabel kalender. Tidak commit."""
    ada = set(db.scalars(select(Kalender.tanggal).where(Kalender.tanggal.between(mulai, selesai))))
    baru = [r for r in baris_kalender(mulai, selesai) if r["tanggal"] not in ada]
    if baru:
        db.bulk_insert_mappings(Kalender, baru)
    return len(baru)


def pastikan_jadwal(db: Session, kombinasi: Iterable[tuple[str, str, str]]) -> int:
    """kombinasi: (id_rute, jam_keberangkatan, layanan). Tambahkan yang belum ada. Tidak commit."""
    ada = set(db.execute(select(Jadwal.id_rute, Jadwal.jam_keberangkatan, Jadwal.layanan)).tuples())
    baru = [k for k in set(kombinasi) if k not in ada]
    for id_rute, jam, layanan in baru:
        db.add(Jadwal(id_rute=id_rute, jam_keberangkatan=jam, layanan=layanan))
    if baru:
        db.flush()
    return len(baru)
