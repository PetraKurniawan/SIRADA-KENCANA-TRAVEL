"""
Router /api/tindak-lanjut — CRUD tindak lanjut analisis.

Akses:
- Admin / Owner : melihat dan mengelola semua tindakan.
- KepalaOutlet  : hanya tindakan yang id_rute-nya berasal dari cabangnya,
                  atau yang ia buat sendiri (bila id_rute kosong).
"""
from datetime import timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import cabang_scope, get_current_user
from app.models.models import Pengguna, Rute, TindakLanjut, utcnow
from app.schemas.tindak_lanjut import TindakLanjutIn, TindakLanjutOut, TindakLanjutUpdate

router = APIRouter(prefix="/api/tindak-lanjut", tags=["Tindak Lanjut"])


def _scope_cabang(user: Pengguna, db: Session) -> Optional[str]:
    """Nama cabang yang boleh dilihat user; None = semua cabang."""
    return cabang_scope(user, db)


def _ambil(db: Session, id: str) -> TindakLanjut:
    item = db.query(TindakLanjut).filter(TindakLanjut.id == id).first()
    if item is None:
        raise HTTPException(status_code=404, detail="Tindak lanjut tidak ditemukan")
    return item


def _cek_akses_baca(item: TindakLanjut, user: Pengguna, db: Session):
    """Kepala Outlet hanya boleh baca tindakan dari cabangnya atau miliknya sendiri."""
    scope = _scope_cabang(user, db)
    if scope is None:
        return  # Admin/Owner: bebas
    if item.id_pembuat == user.id_pengguna:
        return  # pembuat sendiri
    if item.rute and item.rute.cabang_asal == scope:
        return
    raise HTTPException(status_code=403, detail="Tidak punya akses ke tindak lanjut ini")


def _cek_akses_tulis(item: TindakLanjut, user: Pengguna, db: Session):
    """Owner hanya boleh membuat/memperbarui tindakan cabangnya (sama dengan Kepala Outlet)."""
    if user.role == "Admin":
        return
    scope = _scope_cabang(user, db)
    if scope is None and user.role == "Owner":
        return  # Owner: bebas (tidak dibatasi cabang)
    if item.id_pembuat == user.id_pengguna:
        return
    if item.rute and item.rute.cabang_asal == scope:
        return
    raise HTTPException(status_code=403, detail="Tidak punya akses untuk mengubah tindak lanjut ini")


@router.get("", response_model=List[TindakLanjutOut])
def daftar(
    status: Optional[str] = Query(None, description="baru|diproses|selesai|ditolak"),
    id_rute: Optional[str] = Query(None),
    cabang: Optional[str] = Query(None, description="Filter nama cabang (Admin/Owner)"),
    db: Session = Depends(get_db),
    user: Pengguna = Depends(get_current_user),
):
    """Daftar tindak lanjut. Kepala Outlet hanya melihat miliknya atau cabangnya."""
    scope = _scope_cabang(user, db)
    q = db.query(TindakLanjut)
    if scope:
        # id_rute-nya dari cabang scope ATAU pembuat adalah diri sendiri
        q = q.outerjoin(Rute, TindakLanjut.id_rute == Rute.id_rute).filter(
            (Rute.cabang_asal == scope) | (TindakLanjut.id_pembuat == user.id_pengguna)
        )
    elif cabang:
        q = q.outerjoin(Rute, TindakLanjut.id_rute == Rute.id_rute).filter(Rute.cabang_asal == cabang)
    if status:
        q = q.filter(TindakLanjut.status == status)
    if id_rute:
        q = q.filter(TindakLanjut.id_rute == id_rute)
    return q.order_by(TindakLanjut.dibuat_pada.desc()).all()


@router.post("", response_model=TindakLanjutOut, status_code=201)
def buat(
    payload: TindakLanjutIn,
    db: Session = Depends(get_db),
    user: Pengguna = Depends(get_current_user),
):
    """Buat tindak lanjut baru."""
    # Validasi id_rute jika diberikan
    rute = None
    if payload.id_rute:
        rute = db.query(Rute).filter(Rute.id_rute == payload.id_rute).first()
        if rute is None:
            raise HTTPException(status_code=400, detail="Rute tidak ditemukan")
        # Kepala Outlet hanya boleh untuk rute cabangnya
        scope = _scope_cabang(user, db)
        if scope and rute.cabang_asal != scope:
            raise HTTPException(status_code=403, detail="Rute bukan milik cabang Anda")

    item = TindakLanjut(
        id_rute=payload.id_rute,
        sumber=payload.sumber,
        judul=payload.judul,
        deskripsi=payload.deskripsi,
        prioritas=payload.prioritas,
        status=payload.status,
        id_pembuat=user.id_pengguna,
        catatan=payload.catatan,
        tenggat=payload.tenggat,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.get("/{id}", response_model=TindakLanjutOut)
def ambil(id: str, db: Session = Depends(get_db), user: Pengguna = Depends(get_current_user)):
    item = _ambil(db, id)
    _cek_akses_baca(item, user, db)
    return item


@router.put("/{id}", response_model=TindakLanjutOut)
def perbarui(
    id: str,
    payload: TindakLanjutUpdate,
    db: Session = Depends(get_db),
    user: Pengguna = Depends(get_current_user),
):
    item = _ambil(db, id)
    _cek_akses_tulis(item, user, db)

    for field, val in payload.model_dump(exclude_unset=True).items():
        setattr(item, field, val)

    # Set selesai_pada otomatis
    if payload.status == "selesai" and item.selesai_pada is None:
        item.selesai_pada = utcnow()
    elif payload.status in ("baru", "diproses", "ditolak"):
        item.selesai_pada = None

    db.commit()
    db.refresh(item)
    return item


@router.delete("/{id}", status_code=204)
def hapus(id: str, db: Session = Depends(get_db), user: Pengguna = Depends(get_current_user)):
    item = _ambil(db, id)
    _cek_akses_tulis(item, user, db)
    db.delete(item)
    db.commit()
