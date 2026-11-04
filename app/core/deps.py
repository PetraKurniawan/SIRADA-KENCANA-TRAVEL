from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from app.core.database import get_db
from app.core.security import decode_access_token
from app.models.models import Cabang, Pengguna

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/auth/login")


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> Pengguna:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Token tidak valid atau kedaluwarsa",
        headers={"WWW-Authenticate": "Bearer"},
    )
    payload = decode_access_token(token)
    if payload is None:
        raise credentials_exception
    email = payload.get("sub")
    user = db.query(Pengguna).filter(Pengguna.email == email).first()
    # Token lama tidak berlaku lagi bila akun sudah ditolak/dinonaktifkan Admin.
    if user is None or user.status != "aktif":
        raise credentials_exception
    return user


def require_roles(*roles):
    """Dependency factory: batasi endpoint hanya untuk role tertentu.
    Contoh: Depends(require_roles('Admin'))"""
    def checker(user: Pengguna = Depends(get_current_user)) -> Pengguna:
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="Tidak punya akses untuk aksi ini")
        return user
    return checker


def cabang_scope(user: Pengguna, db: Session) -> str | None:
    """Nama cabang yang boleh dilihat user. None = semua cabang (Admin & Owner).
    Kepala Outlet hanya melihat transaksi/rute yang berangkat dari cabangnya
    (kolom transaksi.cabang_asal / rute.cabang_asal)."""
    if user.role != "KepalaOutlet":
        return None
    cabang = db.get(Cabang, user.id_cabang) if user.id_cabang else None
    # Kepala Outlet tanpa cabang tidak boleh melihat data apa pun.
    return cabang.nama_cabang if cabang else "__tanpa_cabang__"


def get_cabang_scope(user: Pengguna = Depends(get_current_user),
                     db: Session = Depends(get_db)) -> str | None:
    """Dependency: dipakai endpoint data untuk membatasi Kepala Outlet ke cabangnya."""
    return cabang_scope(user, db)
