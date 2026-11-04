from typing import List, Optional
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.core.deps import require_roles
from app.models.models import Cabang, Pengguna
from app.schemas.schemas import PenggunaOut, SetujuiPenggunaRequest
from app.services import email_service

router = APIRouter(prefix="/api/pengguna", tags=["Manajemen Pengguna"])


def _ambil(db: Session, id_pengguna: str) -> Pengguna:
    user = db.query(Pengguna).filter(Pengguna.id_pengguna == id_pengguna).first()
    if user is None:
        raise HTTPException(status_code=404, detail="Pengguna tidak ditemukan")
    return user


@router.get("", response_model=List[PenggunaOut])
def daftar_pengguna(status: Optional[str] = Query(None, description="menunggu | aktif | ditolak"),
                    db: Session = Depends(get_db), _admin: Pengguna = Depends(require_roles("Admin"))):
    """Daftar akun untuk halaman Kelola Pengguna. Pendaftar terbaru di atas."""
    q = db.query(Pengguna)
    if status:
        q = q.filter(Pengguna.status == status)
    return q.order_by(Pengguna.dibuat_pada.desc().nullslast(), Pengguna.nama).all()


@router.get("/cabang")
def daftar_cabang(db: Session = Depends(get_db), _admin: Pengguna = Depends(require_roles("Admin"))):
    """Pilihan cabang saat menyetujui akun Kepala Outlet."""
    return [{"id_cabang": c.id_cabang, "nama_cabang": c.nama_cabang} for c in db.query(Cabang).all()]


@router.post("/{id_pengguna}/setujui", response_model=PenggunaOut)
def setujui(id_pengguna: str, payload: SetujuiPenggunaRequest, bg: BackgroundTasks,
            db: Session = Depends(get_db), _admin: Pengguna = Depends(require_roles("Admin"))):
    user = _ambil(db, id_pengguna)
    if not user.email_terverifikasi:
        raise HTTPException(status_code=400, detail="Pengguna belum memverifikasi emailnya.")
    if payload.role == "KepalaOutlet":
        if not payload.id_cabang or not db.query(Cabang).filter(Cabang.id_cabang == payload.id_cabang).first():
            raise HTTPException(status_code=400, detail="Pilih cabang untuk Kepala Outlet.")
    user.role = payload.role
    user.id_cabang = payload.id_cabang if payload.role == "KepalaOutlet" else None
    user.status = "aktif"
    db.commit()
    bg.add_task(email_service.kirim_akun_disetujui, user.email, user.nama, user.role)
    return user


@router.post("/{id_pengguna}/tolak", response_model=PenggunaOut)
def tolak(id_pengguna: str, bg: BackgroundTasks, db: Session = Depends(get_db),
          admin: Pengguna = Depends(require_roles("Admin"))):
    user = _ambil(db, id_pengguna)
    if user.id_pengguna == admin.id_pengguna:
        raise HTTPException(status_code=400, detail="Tidak bisa menolak akun Anda sendiri.")
    if user.status != "menunggu":
        raise HTTPException(status_code=400, detail="Hanya pendaftar berstatus menunggu yang bisa ditolak.")
    user.status = "ditolak"
    db.commit()
    bg.add_task(email_service.kirim_akun_ditolak, user.email, user.nama)
    return user
