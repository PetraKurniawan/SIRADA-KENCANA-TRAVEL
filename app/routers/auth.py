from datetime import timedelta
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from app.core import config
from app.core.database import get_db
from app.core.rate_limit import (
    catat_login_gagal, cek_login_diblokir, cek_rate_limit, ip_klien, reset_login_gagal,
)
from app.core.security import verify_password, create_access_token, hash_password
from app.models.models import Pengguna
from app.schemas.schemas import (
    EmailRequest, LoginRequest, LoginResponse, PesanResponse, RegisterRequest,
    ResetPasswordRequest, TokenRequest,
)
from app.services import email_service
from app.services import token_service as tokens

router = APIRouter(prefix="/api/auth", tags=["Auth"])

# Pesan sengaja dibuat sama apa pun kondisinya, supaya endpoint tidak bisa dipakai
# untuk menebak email mana yang terdaftar.
PESAN_DAFTAR = "Pendaftaran diterima. Cek email Anda untuk link verifikasi."
PESAN_KIRIM_ULANG = "Jika email terdaftar dan belum diverifikasi, link verifikasi baru telah dikirim."
PESAN_LUPA = "Jika email terdaftar, link untuk mengatur ulang kata sandi telah dikirim."


def _kirim_verifikasi(db: Session, user: Pengguna, bg: BackgroundTasks):
    token = tokens.buat_token(db, user, tokens.VERIFIKASI_EMAIL,
                              timedelta(hours=config.VERIFIKASI_EMAIL_EXPIRE_JAM))
    bg.add_task(email_service.kirim_verifikasi_email, user.email, user.nama, token)


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)):
    ip = ip_klien(request)
    # Cek blokir SEBELUM memeriksa password, supaya tebakan yang kebetulan benar pun ditolak.
    cek_login_diblokir(payload.email, ip)
    user = db.query(Pengguna).filter(Pengguna.email == payload.email).first()
    if not user or not verify_password(payload.password, user.password_hash):
        catat_login_gagal(payload.email, ip)
        raise HTTPException(status_code=401, detail="Email atau kata sandi salah")
    reset_login_gagal(payload.email)
    # Status akun baru dicek SETELAH password benar, agar tidak membocorkan info akun.
    if not user.email_terverifikasi:
        raise HTTPException(status_code=403,
                            detail="Email belum diverifikasi. Cek kotak masuk Anda untuk link verifikasi.")
    if user.status == "menunggu":
        raise HTTPException(status_code=403, detail="Akun Anda sedang menunggu persetujuan Admin.")
    if user.status != "aktif":
        raise HTTPException(status_code=403, detail="Akun Anda tidak aktif. Hubungi Admin.")
    token = create_access_token({"sub": user.email, "role": user.role})
    return LoginResponse(access_token=token, nama=user.nama, role=user.role)


@router.post("/register", response_model=PesanResponse)
def register(payload: RegisterRequest, request: Request, bg: BackgroundTasks, db: Session = Depends(get_db)):
    """Daftar akun baru. Akun baru belum punya role & berstatus 'menunggu' sampai
    emailnya diverifikasi DAN disetujui Admin."""
    # Dihitung sebelum cek database, jadi respons 429 tidak membocorkan email terdaftar atau tidak.
    cek_rate_limit("register", payload.email, ip_klien(request))
    user = db.query(Pengguna).filter(Pengguna.email == payload.email).first()
    if user is None:
        user = Pengguna(nama=payload.nama.strip(), email=payload.email,
                        password_hash=hash_password(payload.password), role="",
                        email_terverifikasi=False, status="menunggu")
        db.add(user)
        db.commit()
        _kirim_verifikasi(db, user, bg)
    elif not user.email_terverifikasi:
        # Daftar ulang dengan email yang belum diverifikasi: kirim ulang link saja,
        # data akun tidak ditimpa (bisa jadi yang mendaftar ulang bukan pemilik email).
        _kirim_verifikasi(db, user, bg)
    return PesanResponse(pesan=PESAN_DAFTAR)


@router.post("/verifikasi-email", response_model=PesanResponse)
def verifikasi_email(payload: TokenRequest, db: Session = Depends(get_db)):
    user = tokens.pakai_token(db, payload.token, tokens.VERIFIKASI_EMAIL)
    if user is None:
        raise HTTPException(status_code=400,
                            detail="Link verifikasi tidak valid atau sudah kedaluwarsa. Minta link baru.")
    user.email_terverifikasi = True
    db.commit()
    if user.status == "aktif":
        return PesanResponse(pesan="Email berhasil diverifikasi. Silakan masuk.")
    return PesanResponse(pesan="Email berhasil diverifikasi. Akun Anda sekarang menunggu persetujuan Admin.")


@router.post("/kirim-ulang-verifikasi", response_model=PesanResponse)
def kirim_ulang_verifikasi(payload: EmailRequest, request: Request, bg: BackgroundTasks,
                           db: Session = Depends(get_db)):
    cek_rate_limit("kirim_ulang_verifikasi", payload.email, ip_klien(request))
    user = db.query(Pengguna).filter(Pengguna.email == payload.email).first()
    if user and not user.email_terverifikasi:
        _kirim_verifikasi(db, user, bg)
    return PesanResponse(pesan=PESAN_KIRIM_ULANG)


@router.post("/lupa-password", response_model=PesanResponse)
def lupa_password(payload: EmailRequest, request: Request, bg: BackgroundTasks, db: Session = Depends(get_db)):
    cek_rate_limit("lupa_password", payload.email, ip_klien(request))
    user = db.query(Pengguna).filter(Pengguna.email == payload.email).first()
    # Hanya untuk email yang sudah terbukti milik pengguna & akun yang tidak ditolak.
    if user and user.email_terverifikasi and user.status != "ditolak":
        token = tokens.buat_token(db, user, tokens.RESET_PASSWORD,
                                  timedelta(minutes=config.RESET_PASSWORD_EXPIRE_MENIT))
        bg.add_task(email_service.kirim_reset_password, user.email, user.nama, token)
    return PesanResponse(pesan=PESAN_LUPA)


@router.post("/reset-password", response_model=PesanResponse)
def reset_password(payload: ResetPasswordRequest, db: Session = Depends(get_db)):
    user = tokens.pakai_token(db, payload.token, tokens.RESET_PASSWORD)
    if user is None:
        raise HTTPException(status_code=400,
                            detail="Link reset tidak valid atau sudah kedaluwarsa. Minta link baru.")
    user.password_hash = hash_password(payload.password_baru)
    db.commit()
    return PesanResponse(pesan="Kata sandi berhasil diubah. Silakan masuk dengan kata sandi baru.")
