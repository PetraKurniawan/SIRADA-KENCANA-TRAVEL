"""Pembuatan & validasi token sekali pakai untuk link verifikasi email dan reset password."""
import hashlib
import secrets
from datetime import timedelta
from sqlalchemy.orm import Session
from app.models.models import Pengguna, TokenPengguna, utcnow

VERIFIKASI_EMAIL = "verifikasi_email"
RESET_PASSWORD = "reset_password"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def buat_token(db: Session, user: Pengguna, jenis: str, berlaku: timedelta) -> str:
    """Buat token baru & batalkan token lama sejenis milik user (hanya link terbaru yang berlaku).
    Mengembalikan token mentah untuk dikirim lewat email; yang disimpan hanya hash-nya."""
    sekarang = utcnow()
    (db.query(TokenPengguna)
       .filter(TokenPengguna.id_pengguna == user.id_pengguna, TokenPengguna.jenis == jenis,
               TokenPengguna.dipakai_pada.is_(None))
       .update({TokenPengguna.dipakai_pada: sekarang}, synchronize_session=False))

    token = secrets.token_urlsafe(32)
    db.add(TokenPengguna(id_pengguna=user.id_pengguna, token_hash=_hash(token), jenis=jenis,
                         kedaluwarsa=sekarang + berlaku))
    db.commit()
    return token


def pakai_token(db: Session, token: str, jenis: str) -> Pengguna | None:
    """Validasi token lalu tandai sudah dipakai. None bila token salah/kedaluwarsa/sudah dipakai.
    Tidak melakukan commit — pemanggil meng-commit bersama perubahan pada user."""
    row = (db.query(TokenPengguna)
             .filter(TokenPengguna.token_hash == _hash(token), TokenPengguna.jenis == jenis)
             .first())
    if not row or row.dipakai_pada is not None or row.kedaluwarsa < utcnow():
        return None
    row.dipakai_pada = utcnow()
    return db.query(Pengguna).filter(Pengguna.id_pengguna == row.id_pengguna).first()
