"""
Service Analisis Performa Rute & Cabang - statistik deskriptif + load factor (P.5 pada DFD).

Semua agregasi dikerjakan database (GROUP BY), bukan pandas, supaya cepat juga di Postgres
Railway yang CPU-nya kecil. Satu query dasar dipakai bersama oleh performa rute, performa
cabang, dan perbandingan VIP vs Reguler, lalu hasilnya di-cache singkat di memori.

Definisi:
- Trip = kombinasi unik (tanggal, jam_keberangkatan, layanan) pada satu rute (satu armada).
- Kapasitas per trip = kapasitas armada yang dipakai; bila armada kosong, pakai rata-rata
  kapasitas armada untuk layanan tersebut (VIP 8, Reguler 12 di data).
- Okupansi = rata-rata okupansi per trip = rata-rata (penumpang trip / kapasitas trip) x 100.
  Karena satu trip memakai satu armada, sum(penumpang/kapasitas) per kelompok armada sama
  dengan jumlah rasio per trip, jadi tidak perlu GROUP BY per trip (yang berat di SQLite).
"""
import threading
import time
from datetime import date
from typing import Optional

from sqlalchemy import String, cast, func, literal
from sqlalchemy.orm import Session

from app.models.models import Armada, Rute, Transaksi

# Kategori status okupansi (sesuai laporan): >= 80 Tinggi, 50-80 Sedang, < 50 Rendah.
BATAS_TINGGI = 80.0
BATAS_SEDANG = 50.0

_CACHE_TTL_DETIK = 300
_cache: dict = {}
_cache_lock = threading.Lock()


def kategori_status(okupansi: float) -> str:
    if okupansi >= BATAS_TINGGI:
        return "Tinggi"
    if okupansi >= BATAS_SEDANG:
        return "Sedang"
    return "Rendah"


def _kapasitas_armada(db: Session) -> tuple[dict, dict]:
    """(kapasitas per id_armada, rata-rata kapasitas per tipe layanan)."""
    per_armada, per_layanan = {}, {}
    for a in db.query(Armada.id_armada, Armada.tipe_layanan, Armada.kapasitas).all():
        per_armada[a.id_armada] = a.kapasitas
        per_layanan.setdefault(a.tipe_layanan, []).append(a.kapasitas)
    rata = {k: sum(v) / len(v) for k, v in per_layanan.items() if v}
    return per_armada, rata


def _kapasitas_fallback(layanan: str, rata_layanan: dict) -> float:
    if layanan in rata_layanan:
        return rata_layanan[layanan]
    if rata_layanan:  # layanan tak dikenal: rata-rata seluruh armada
        return sum(rata_layanan.values()) / len(rata_layanan)
    return 0.0


def _sidik_data(db: Session) -> tuple:
    """Penanda murah untuk invalidasi cache: berubah bila transaksi/armada bertambah/berubah."""
    n_trx = db.query(func.count(Transaksi.id_transaksi)).scalar() or 0
    arm = db.query(func.count(Armada.id_armada), func.coalesce(func.sum(Armada.kapasitas), 0)).one()
    n_rute = db.query(func.count(Rute.id_rute)).scalar() or 0
    return (n_trx, arm[0], int(arm[1]), n_rute)


def _kelompok_dasar(db: Session, cabang: Optional[str], tanggal_mulai: Optional[date],
                    tanggal_selesai: Optional[date]) -> list[dict]:
    """Agregasi penumpang per (rute, cabang_asal, layanan, armada). Hasil kecil (puluhan baris).
    cabang_asal ikut di GROUP BY, jadi scope Kepala Outlet cukup menyaring hasil agregasi semua
    cabang (satu entri cache dipakai bersama Admin, Owner, dan semua Kepala Outlet)."""
    semua = _kelompok_semua(db, tanggal_mulai, tanggal_selesai)
    return [k for k in semua if k["cabang"] == cabang] if cabang else semua


def _kelompok_semua(db: Session, tanggal_mulai: Optional[date],
                    tanggal_selesai: Optional[date]) -> list[dict]:
    kunci = (tanggal_mulai, tanggal_selesai)
    sidik = _sidik_data(db)
    sekarang = time.monotonic()
    with _cache_lock:
        ada = _cache.get(kunci)
        if ada and ada[0] == sidik and sekarang - ada[1] < _CACHE_TTL_DETIK:
            return ada[2]

    # Kunci trip di dalam kelompok armada: tanggal + jam (layanan & armada sudah di GROUP BY).
    kunci_trip = cast(Transaksi.tanggal, String) + literal("|") + Transaksi.jam_keberangkatan
    q = (db.query(Transaksi.id_rute, Transaksi.cabang_asal, Transaksi.layanan, Transaksi.id_armada,
                  func.count(func.distinct(kunci_trip)).label("trip"),
                  func.count(Transaksi.id_transaksi).label("transaksi"),
                  func.coalesce(func.sum(Transaksi.jumlah_unit), 0).label("penumpang"),
                  func.coalesce(func.sum(Transaksi.total_bayar), 0).label("pendapatan"),
                  func.coalesce(func.sum(Transaksi.harga_satuan), 0).label("jumlah_harga"))
           .filter(Transaksi.jenis_transaksi == "Penumpang"))
    if tanggal_mulai:
        q = q.filter(Transaksi.tanggal >= tanggal_mulai)
    if tanggal_selesai:
        q = q.filter(Transaksi.tanggal <= tanggal_selesai)
    q = q.group_by(Transaksi.id_rute, Transaksi.cabang_asal, Transaksi.layanan, Transaksi.id_armada)

    per_armada, rata_layanan = _kapasitas_armada(db)
    hasil = []
    for r in q.all():
        kap = per_armada.get(r.id_armada) or _kapasitas_fallback(r.layanan, rata_layanan)
        penumpang = float(r.penumpang)
        hasil.append({
            "id_rute": r.id_rute, "cabang": r.cabang_asal, "layanan": r.layanan,
            "trip": int(r.trip), "transaksi": int(r.transaksi), "penumpang": penumpang,
            "pendapatan": float(r.pendapatan), "jumlah_harga": float(r.jumlah_harga),
            "kapasitas_trip": kap,
            "kapasitas_total": kap * int(r.trip),
            # jumlah okupansi per trip pada kelompok ini (penumpang/kapasitas, satu armada)
            "jumlah_rasio": penumpang / kap if kap else 0.0,
        })
    with _cache_lock:
        _cache[kunci] = (sidik, sekarang, hasil)
        if len(_cache) > 64:  # jaga ukuran cache tetap kecil
            for k in list(_cache)[: len(_cache) - 64]:
                _cache.pop(k, None)
    return hasil


def _ringkas(kelompok: list[dict]) -> dict:
    trip = sum(k["trip"] for k in kelompok)
    transaksi = sum(k["transaksi"] for k in kelompok)
    penumpang = sum(k["penumpang"] for k in kelompok)
    pendapatan = sum(k["pendapatan"] for k in kelompok)
    kapasitas = sum(k["kapasitas_total"] for k in kelompok)
    rasio = sum(k["jumlah_rasio"] for k in kelompok)
    okupansi = round(100 * rasio / trip, 1) if trip else 0.0

    per_layanan: dict[str, int] = {}
    for k in kelompok:
        per_layanan[k["layanan"]] = per_layanan.get(k["layanan"], 0) + k["transaksi"]
    if per_layanan:
        dominan = max(per_layanan, key=per_layanan.get)
        dominan_persen = round(100 * per_layanan[dominan] / transaksi, 1) if transaksi else 0.0
    else:
        dominan, dominan_persen = "-", 0.0

    return {
        "total_trip": trip,
        "total_transaksi": transaksi,
        "total_penumpang": int(round(penumpang)),
        "total_pendapatan": pendapatan,
        "kapasitas_tersedia": int(round(kapasitas)),
        "okupansi_persen": okupansi,
        "layanan_dominan": dominan,
        "layanan_dominan_persen": dominan_persen,
        "rata_penumpang_per_trip": round(penumpang / trip, 2) if trip else 0.0,
        "status": kategori_status(okupansi),
    }


def _tambah_kontribusi(hasil: list[dict]) -> list[dict]:
    total = sum(h["total_pendapatan"] for h in hasil)
    for h in hasil:
        h["kontribusi_pendapatan_persen"] = round(100 * h["total_pendapatan"] / total, 1) if total else 0.0
    hasil.sort(key=lambda x: -x["total_pendapatan"])
    return hasil


def analisis_performa_rute(db: Session, cabang: Optional[str] = None,
                           tanggal_mulai: Optional[date] = None,
                           tanggal_selesai: Optional[date] = None) -> list[dict]:
    """Performa per rute. cabang = scope Kepala Outlet (transaksi.cabang_asal), None = semua."""
    kelompok = _kelompok_dasar(db, cabang, tanggal_mulai, tanggal_selesai)
    rute_q = db.query(Rute.id_rute, Rute.nama_rute, Rute.cabang_asal, Rute.cabang_tujuan)
    if cabang:
        rute_q = rute_q.filter(Rute.cabang_asal == cabang)
    info_rute = {r.id_rute: r for r in rute_q.all()}

    per_rute: dict[str, list] = {}
    for k in kelompok:
        per_rute.setdefault(k["id_rute"], []).append(k)

    hasil = []
    for id_rute, grp in per_rute.items():
        info = info_rute.get(id_rute)
        if info is None and cabang:
            continue
        hasil.append({
            "id_rute": id_rute,
            "rute": info.nama_rute if info else id_rute,
            "cabang_asal": info.cabang_asal if info else grp[0]["cabang"],
            "cabang_tujuan": info.cabang_tujuan if info else None,
            **_ringkas(grp),
        })
    return _tambah_kontribusi(hasil)


def analisis_performa_cabang(db: Session, cabang: Optional[str] = None,
                             tanggal_mulai: Optional[date] = None,
                             tanggal_selesai: Optional[date] = None) -> list[dict]:
    """Performa per cabang asal keberangkatan (transaksi.cabang_asal)."""
    kelompok = _kelompok_dasar(db, cabang, tanggal_mulai, tanggal_selesai)
    per_cabang: dict[str, list] = {}
    for k in kelompok:
        per_cabang.setdefault(k["cabang"], []).append(k)
    hasil = []
    for nama, grp in per_cabang.items():
        hasil.append({"cabang": nama, "jumlah_rute": len({k["id_rute"] for k in grp}), **_ringkas(grp)})
    return _tambah_kontribusi(hasil)


def perbandingan_vip_reguler(db: Session, cabang: Optional[str] = None,
                             tanggal_mulai: Optional[date] = None,
                             tanggal_selesai: Optional[date] = None) -> list[dict]:
    kelompok = _kelompok_dasar(db, cabang, tanggal_mulai, tanggal_selesai)
    total_trx = sum(k["transaksi"] for k in kelompok)
    per_layanan: dict[str, list] = {}
    for k in kelompok:
        per_layanan.setdefault(k["layanan"], []).append(k)
    hasil = []
    for layanan, grp in sorted(per_layanan.items()):
        ringkas = _ringkas(grp)
        n = ringkas["total_transaksi"]
        hasil.append({
            "layanan": layanan,
            "share_persen": round(100 * n / total_trx, 1) if total_trx else 0.0,
            "rata_harga_tiket": round(sum(k["jumlah_harga"] for k in grp) / n, 0) if n else 0.0,
            "total_pendapatan": ringkas["total_pendapatan"],
            "total_penumpang": ringkas["total_penumpang"],
            "total_trip": ringkas["total_trip"],
            "okupansi_persen": ringkas["okupansi_persen"],
        })
    return hasil


def panaskan_cache() -> None:
    """Opsional: panggil sekali saat startup (di thread latar) supaya buka halaman Performa
    pertama kali tidak menunggu agregasi penuh. Aman bila gagal."""
    from app.core.database import SessionLocal
    db = SessionLocal()
    try:
        _kelompok_semua(db, None, None)
    except Exception:  # noqa: BLE001 - hanya optimasi, jangan ganggu startup
        pass
    finally:
        db.close()
