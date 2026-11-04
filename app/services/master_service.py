"""
Service Data Master: Rute, Armada, Jadwal (CRUD) dan Cabang (baca saja).

Aturan akses (dicek di sini, router hanya meneruskan scope):
- scope None  -> Admin: boleh mengelola semua baris.
- scope "X"   -> Kepala Outlet cabang X: hanya rute dengan cabang_asal X, armada dengan
                 basis_outlet X, dan jadwal yang rutenya berangkat dari X. Selain itu 403.
- Owner hanya membaca (dibatasi di router).
Hapus ditolak (409) bila baris masih dirujuk transaksi.
"""
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.models import Armada, Cabang, Jadwal, Rute, Transaksi
from app.schemas.master import ArmadaIn, JadwalIn, RuteIn


# ---------------------------------------------------------------- util
def _403(pesan: str = "Anda hanya dapat mengelola data cabang Anda sendiri"):
    raise HTTPException(status_code=403, detail=pesan)


def _404(apa: str):
    raise HTTPException(status_code=404, detail=f"{apa} tidak ditemukan")


def _cek_scope(scope: Optional[str], cabang: Optional[str]):
    if scope is not None and cabang != scope:
        _403()


def _cabang_ada(db: Session, nama: str) -> str:
    """Kembalikan nama cabang resmi (abaikan beda huruf besar/kecil) atau 422."""
    c = db.query(Cabang).filter(func.lower(Cabang.nama_cabang) == nama.strip().lower()).first()
    if not c:
        daftar = ", ".join(x for (x,) in db.query(Cabang.nama_cabang).order_by(Cabang.nama_cabang))
        raise HTTPException(status_code=422, detail=f"Cabang '{nama}' tidak terdaftar (pilihan: {daftar})")
    return c.nama_cabang


def _commit(db: Session, pesan_konflik: str):
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail=pesan_konflik)


def _ada_transaksi(db: Session, *kondisi) -> int:
    """Jumlah transaksi yang cocok (cukup tahu ada/tidak; dibatasi supaya cepat)."""
    return db.query(Transaksi.id_transaksi).filter(*kondisi).limit(1).count()


def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", ".")


# ---------------------------------------------------------------- cabang
def list_cabang(db: Session, scope: Optional[str] = None) -> list[dict]:
    n_rute = dict(db.query(Rute.cabang_asal, func.count(Rute.id_rute)).group_by(Rute.cabang_asal).all())
    n_armada = dict(db.query(Armada.basis_outlet, func.count(Armada.id_armada))
                      .group_by(Armada.basis_outlet).all())
    return [{"id_cabang": c.id_cabang, "nama_cabang": c.nama_cabang, "kota": c.kota,
             "jumlah_rute": n_rute.get(c.nama_cabang, 0), "jumlah_armada": n_armada.get(c.nama_cabang, 0),
             "cabang_saya": scope is not None and c.nama_cabang == scope}
            for c in db.query(Cabang).order_by(Cabang.nama_cabang).all()]


# ---------------------------------------------------------------- rute
def _rute_out(r: Rute, n_jadwal: int = 0, n_trx: int = 0) -> dict:
    return {"id_rute": r.id_rute, "nama_rute": r.nama_rute, "cabang_asal": r.cabang_asal,
            "cabang_tujuan": r.cabang_tujuan, "layanan_tersedia": r.layanan_tersedia,
            "jumlah_jadwal": n_jadwal, "jumlah_transaksi": n_trx}


def list_rute(db: Session, scope: Optional[str]) -> list[dict]:
    q = db.query(Rute)
    if scope is not None:
        q = q.filter(Rute.cabang_asal == scope)
    rute = q.order_by(Rute.nama_rute).all()
    n_jadwal = dict(db.query(Jadwal.id_rute, func.count(Jadwal.id_jadwal)).group_by(Jadwal.id_rute).all())
    trx_q = db.query(Transaksi.id_rute, func.count(Transaksi.id_transaksi)).group_by(Transaksi.id_rute)
    if scope is not None:
        trx_q = trx_q.filter(Transaksi.id_rute.in_([r.id_rute for r in rute] or [""]))
    n_trx = dict(trx_q.all())
    return [_rute_out(r, n_jadwal.get(r.id_rute, 0), n_trx.get(r.id_rute, 0)) for r in rute]


def _get_rute(db: Session, id_rute: str, scope: Optional[str]) -> Rute:
    r = db.get(Rute, id_rute)
    if not r:
        _404("Rute")
    _cek_scope(scope, r.cabang_asal)
    return r


def _validasi_rute(db: Session, data: RuteIn) -> tuple[str, str]:
    asal, tujuan = _cabang_ada(db, data.cabang_asal), _cabang_ada(db, data.cabang_tujuan)
    if asal == tujuan:
        raise HTTPException(status_code=422, detail="Cabang asal dan tujuan tidak boleh sama")
    return asal, tujuan


def create_rute(db: Session, data: RuteIn, scope: Optional[str]) -> dict:
    asal, tujuan = _validasi_rute(db, data)
    _cek_scope(scope, asal)
    nama = f"{asal}->{tujuan}"
    if db.query(Rute).filter(Rute.nama_rute == nama).first():
        raise HTTPException(status_code=409, detail=f"Rute {nama} sudah ada")
    r = Rute(nama_rute=nama, cabang_asal=asal, cabang_tujuan=tujuan,
             layanan_tersedia=data.layanan_tersedia)
    db.add(r)
    _commit(db, f"Rute {nama} sudah ada")
    db.refresh(r)
    return _rute_out(r)


def update_rute(db: Session, id_rute: str, data: RuteIn, scope: Optional[str]) -> dict:
    r = _get_rute(db, id_rute, scope)
    asal, tujuan = _validasi_rute(db, data)
    _cek_scope(scope, asal)  # Kepala Outlet tidak boleh memindahkan rute ke cabang lain
    nama = f"{asal}->{tujuan}"

    n_trx = _ada_transaksi(db, Transaksi.id_rute == r.id_rute)
    if (asal, tujuan) != (r.cabang_asal, r.cabang_tujuan):
        if n_trx:
            raise HTTPException(status_code=409, detail=(
                f"Asal/tujuan rute {r.nama_rute} tidak dapat diubah karena sudah dipakai transaksi. "
                "Buat rute baru bila diperlukan."))
        if db.query(Rute).filter(Rute.nama_rute == nama, Rute.id_rute != r.id_rute).first():
            raise HTTPException(status_code=409, detail=f"Rute {nama} sudah ada")

    baru = set(data.layanan_tersedia.split(","))
    dipakai = {l for (l,) in db.query(Jadwal.layanan).filter(Jadwal.id_rute == r.id_rute).distinct()}
    hilang = sorted(dipakai - baru)
    if hilang:
        raise HTTPException(status_code=409, detail=(
            f"Layanan {', '.join(hilang)} masih punya jadwal di rute ini. Hapus jadwalnya dulu."))

    r.nama_rute, r.cabang_asal, r.cabang_tujuan = nama, asal, tujuan
    r.layanan_tersedia = data.layanan_tersedia
    _commit(db, f"Rute {nama} sudah ada")
    db.refresh(r)
    n_jadwal = db.query(func.count(Jadwal.id_jadwal)).filter(Jadwal.id_rute == r.id_rute).scalar() or 0
    return _rute_out(r, n_jadwal)


def delete_rute(db: Session, id_rute: str, scope: Optional[str]) -> dict:
    r = _get_rute(db, id_rute, scope)
    n = db.query(func.count(Transaksi.id_transaksi)).filter(Transaksi.id_rute == r.id_rute).scalar() or 0
    if n:
        raise HTTPException(status_code=409, detail=(
            f"Rute {r.nama_rute} tidak dapat dihapus karena masih dipakai {_fmt(n)} transaksi."))
    # Jadwal milik rute ini ikut terhapus (tidak ada transaksi yang merujuknya).
    db.query(Jadwal).filter(Jadwal.id_rute == r.id_rute).delete(synchronize_session=False)
    db.delete(r)
    _commit(db, f"Rute {r.nama_rute} masih dirujuk data lain")
    return {"pesan": f"Rute {r.nama_rute} dihapus"}


# ---------------------------------------------------------------- armada
def _armada_out(a: Armada) -> dict:
    return {"id_armada": a.id_armada, "kode_armada": a.kode_armada, "jenis_kendaraan": a.jenis_kendaraan,
            "tipe_layanan": a.tipe_layanan, "kapasitas": a.kapasitas, "basis_outlet": a.basis_outlet}


def list_armada(db: Session, scope: Optional[str]) -> list[dict]:
    q = db.query(Armada)
    if scope is not None:
        q = q.filter(Armada.basis_outlet == scope)
    return [_armada_out(a) for a in q.order_by(Armada.basis_outlet, Armada.kode_armada).all()]


def _get_armada(db: Session, id_armada: str, scope: Optional[str]) -> Armada:
    a = db.get(Armada, id_armada)
    if not a:
        _404("Armada")
    _cek_scope(scope, a.basis_outlet)
    return a


def create_armada(db: Session, data: ArmadaIn, scope: Optional[str]) -> dict:
    basis = _cabang_ada(db, data.basis_outlet)
    _cek_scope(scope, basis)
    if db.query(Armada).filter(Armada.kode_armada == data.kode_armada).first():
        raise HTTPException(status_code=409, detail=f"Kode armada {data.kode_armada} sudah dipakai")
    a = Armada(kode_armada=data.kode_armada, jenis_kendaraan=data.jenis_kendaraan,
               tipe_layanan=data.tipe_layanan, kapasitas=data.kapasitas, basis_outlet=basis)
    db.add(a)
    _commit(db, f"Kode armada {data.kode_armada} sudah dipakai")
    db.refresh(a)
    return _armada_out(a)


def update_armada(db: Session, id_armada: str, data: ArmadaIn, scope: Optional[str]) -> dict:
    a = _get_armada(db, id_armada, scope)
    basis = _cabang_ada(db, data.basis_outlet)
    _cek_scope(scope, basis)
    if db.query(Armada).filter(Armada.kode_armada == data.kode_armada,
                               Armada.id_armada != a.id_armada).first():
        raise HTTPException(status_code=409, detail=f"Kode armada {data.kode_armada} sudah dipakai")
    if data.tipe_layanan != a.tipe_layanan and _ada_transaksi(db, Transaksi.id_armada == a.id_armada):
        raise HTTPException(status_code=409, detail=(
            f"Tipe layanan armada {a.kode_armada} tidak dapat diubah karena sudah dipakai transaksi."))
    a.kode_armada, a.jenis_kendaraan = data.kode_armada, data.jenis_kendaraan
    a.tipe_layanan, a.kapasitas, a.basis_outlet = data.tipe_layanan, data.kapasitas, basis
    _commit(db, f"Kode armada {data.kode_armada} sudah dipakai")
    db.refresh(a)
    return _armada_out(a)


def delete_armada(db: Session, id_armada: str, scope: Optional[str]) -> dict:
    a = _get_armada(db, id_armada, scope)
    if _ada_transaksi(db, Transaksi.id_armada == a.id_armada):
        n = db.query(func.count(Transaksi.id_transaksi)).filter(Transaksi.id_armada == a.id_armada).scalar()
        raise HTTPException(status_code=409, detail=(
            f"Armada {a.kode_armada} tidak dapat dihapus karena masih dipakai {_fmt(n)} transaksi."))
    db.delete(a)
    _commit(db, f"Armada {a.kode_armada} masih dirujuk data lain")
    return {"pesan": f"Armada {a.kode_armada} dihapus"}


# ---------------------------------------------------------------- jadwal
def _jadwal_out(j: Jadwal, r: Rute) -> dict:
    return {"id_jadwal": j.id_jadwal, "id_rute": j.id_rute, "nama_rute": r.nama_rute,
            "cabang_asal": r.cabang_asal, "jam_keberangkatan": j.jam_keberangkatan,
            "layanan": j.layanan, "hari_berlaku": j.hari_berlaku}


def list_jadwal(db: Session, scope: Optional[str], id_rute: Optional[str] = None) -> list[dict]:
    q = db.query(Jadwal, Rute).join(Rute, Rute.id_rute == Jadwal.id_rute)
    if scope is not None:
        q = q.filter(Rute.cabang_asal == scope)
    if id_rute:
        q = q.filter(Jadwal.id_rute == id_rute)
    rows = q.order_by(Rute.nama_rute, Jadwal.jam_keberangkatan, Jadwal.layanan).all()
    return [_jadwal_out(j, r) for j, r in rows]


def _get_jadwal(db: Session, id_jadwal: str, scope: Optional[str]) -> tuple[Jadwal, Rute]:
    j = db.get(Jadwal, id_jadwal)
    if not j:
        _404("Jadwal")
    r = db.get(Rute, j.id_rute)
    _cek_scope(scope, r.cabang_asal if r else None)
    return j, r


def _transaksi_jadwal(db: Session, j: Jadwal) -> int:
    return _ada_transaksi(db, Transaksi.id_rute == j.id_rute,
                          Transaksi.jam_keberangkatan == j.jam_keberangkatan,
                          Transaksi.layanan == j.layanan)


def _validasi_jadwal(db: Session, data: JadwalIn, scope: Optional[str]) -> Rute:
    r = db.get(Rute, data.id_rute)
    if not r:
        raise HTTPException(status_code=422, detail="Rute tidak ditemukan")
    _cek_scope(scope, r.cabang_asal)
    tersedia = [x.strip() for x in (r.layanan_tersedia or "").split(",") if x.strip()]
    if tersedia and data.layanan not in tersedia:
        raise HTTPException(status_code=422, detail=(
            f"Layanan {data.layanan} tidak tersedia di rute {r.nama_rute} (tersedia: {', '.join(tersedia)})"))
    return r


def _cek_duplikat_jadwal(db: Session, data: JadwalIn, kecuali: Optional[str] = None):
    q = db.query(Jadwal).filter(Jadwal.id_rute == data.id_rute,
                                Jadwal.jam_keberangkatan == data.jam_keberangkatan,
                                Jadwal.layanan == data.layanan)
    if kecuali:
        q = q.filter(Jadwal.id_jadwal != kecuali)
    if q.first():
        raise HTTPException(status_code=409, detail=(
            f"Jadwal {data.jam_keberangkatan} {data.layanan} untuk rute ini sudah ada"))


def create_jadwal(db: Session, data: JadwalIn, scope: Optional[str]) -> dict:
    r = _validasi_jadwal(db, data, scope)
    _cek_duplikat_jadwal(db, data)
    j = Jadwal(id_rute=r.id_rute, jam_keberangkatan=data.jam_keberangkatan, layanan=data.layanan,
               hari_berlaku=data.hari_berlaku)
    db.add(j)
    _commit(db, "Jadwal yang sama sudah ada")
    db.refresh(j)
    return _jadwal_out(j, r)


def update_jadwal(db: Session, id_jadwal: str, data: JadwalIn, scope: Optional[str]) -> dict:
    j, _ = _get_jadwal(db, id_jadwal, scope)
    r = _validasi_jadwal(db, data, scope)
    kunci_berubah = (data.id_rute, data.jam_keberangkatan, data.layanan) != (
        j.id_rute, j.jam_keberangkatan, j.layanan)
    if kunci_berubah:
        if _transaksi_jadwal(db, j):
            raise HTTPException(status_code=409, detail=(
                "Rute/jam/layanan jadwal ini tidak dapat diubah karena sudah dipakai transaksi. "
                "Hanya hari berlaku yang bisa diubah; buat jadwal baru untuk jam lain."))
        _cek_duplikat_jadwal(db, data, kecuali=j.id_jadwal)
    j.id_rute, j.jam_keberangkatan, j.layanan = data.id_rute, data.jam_keberangkatan, data.layanan
    j.hari_berlaku = data.hari_berlaku
    _commit(db, "Jadwal yang sama sudah ada")
    db.refresh(j)
    return _jadwal_out(j, r)


def delete_jadwal(db: Session, id_jadwal: str, scope: Optional[str]) -> dict:
    j, r = _get_jadwal(db, id_jadwal, scope)
    if _transaksi_jadwal(db, j):
        n = db.query(func.count(Transaksi.id_transaksi)).filter(
            Transaksi.id_rute == j.id_rute, Transaksi.jam_keberangkatan == j.jam_keberangkatan,
            Transaksi.layanan == j.layanan).scalar()
        raise HTTPException(status_code=409, detail=(
            f"Jadwal {j.jam_keberangkatan} {j.layanan} ({r.nama_rute if r else j.id_rute}) tidak dapat "
            f"dihapus karena masih dipakai {_fmt(n)} transaksi."))
    db.delete(j)
    _commit(db, "Jadwal masih dirujuk data lain")
    return {"pesan": f"Jadwal {j.jam_keberangkatan} {j.layanan} dihapus"}
