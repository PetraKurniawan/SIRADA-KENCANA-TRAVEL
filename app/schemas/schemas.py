import re
from pydantic import BaseModel, EmailStr, Field, field_validator
from datetime import date, datetime
from typing import Optional, List, Literal

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _normalisasi_email(v: str) -> str:
    v = v.strip().lower()
    if not _EMAIL_RE.match(v):
        raise ValueError("Format email tidak valid")
    return v


# ---------- AUTH ----------
class LoginRequest(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return v.strip().lower()


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    nama: str
    role: str


class RegisterRequest(BaseModel):
    nama: str = Field(min_length=2, max_length=100)
    email: str
    password: str = Field(min_length=8, max_length=128)

    @field_validator("email")
    @classmethod
    def _cek_email(cls, v: str) -> str:
        return _normalisasi_email(v)


class EmailRequest(BaseModel):
    """Untuk lupa password & kirim ulang verifikasi."""
    email: str

    @field_validator("email")
    @classmethod
    def _cek_email(cls, v: str) -> str:
        return _normalisasi_email(v)


class TokenRequest(BaseModel):
    token: str = Field(min_length=10)


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=10)
    password_baru: str = Field(min_length=8, max_length=128)


class PesanResponse(BaseModel):
    pesan: str


# ---------- MANAJEMEN PENGGUNA (Admin) ----------
RolePengguna = Literal["Admin", "Owner", "KepalaOutlet"]


class PenggunaOut(BaseModel):
    id_pengguna: str
    nama: str
    email: str
    role: str
    id_cabang: Optional[str] = None
    email_terverifikasi: bool
    status: str
    dibuat_pada: Optional[datetime] = None

    class Config:
        from_attributes = True


class SetujuiPenggunaRequest(BaseModel):
    role: RolePengguna
    id_cabang: Optional[str] = None  # wajib untuk KepalaOutlet


# ---------- TRANSAKSI ----------
class TransaksiOut(BaseModel):
    id_transaksi: str
    tanggal: date
    jam_keberangkatan: str
    rute: str
    layanan: str
    channel_pemesanan: str
    jenis_member: Optional[str] = None
    jumlah_unit: float
    satuan: str
    total_bayar: float
    keterangan: Optional[str] = None

    class Config:
        from_attributes = True


class TransaksiListOut(BaseModel):
    total: int
    halaman: int
    per_halaman: int
    data: List[TransaksiOut]


class TransaksiCreate(BaseModel):
    tanggal: date
    jam_keberangkatan: str
    rute: str                     # contoh: "Solo->Semarang"
    layanan: str                  # VIP / Reguler
    jenis_transaksi: str = "Penumpang"
    jenis_member: str = "Non-Member"
    jumlah_unit: float
    satuan: str = "Orang"
    channel_pemesanan: str = "Outlet"
    id_pelanggan: Optional[str] = None


# ---------- DASHBOARD ----------
class DashboardSummary(BaseModel):
    total_transaksi: int
    total_pendapatan: float
    rute_terlaris: str
    okupansi_rute_terlaris: float
    rasio_member: dict
    total_member_aktif: int


# ---------- SEGMENTASI ----------
class SegmentasiRingkasan(BaseModel):
    jenis_member: str
    total_transaksi: int
    total_pendapatan: float
    rata_rata_transaksi: float
    rute_favorit: str
    share_persen: float


class SegmentasiClusterOut(BaseModel):
    cluster: int
    label: str
    jumlah_pelanggan: int
    rata_recency_hari: float
    rata_frequency: float
    rata_monetary: float


# ---------- FORECASTING ----------
class ForecastingRunRequest(BaseModel):
    rute: str
    horizon_hari: int = 30


class ForecastPoint(BaseModel):
    tanggal: date
    aktual: Optional[float] = None
    prediksi: Optional[float] = None


class ForecastingOut(BaseModel):
    rute: str
    model: str
    adf_p_value: float
    prediksi_periode_berikutnya: float
    akurasi_persen: float
    mape_persen: float
    mae: float
    rmse: float
    rekomendasi_unit_tambahan: int
    deret: List[ForecastPoint]


# ---------- PERFORMA RUTE ----------
class PerformaRuteOut(BaseModel):
    rute: str
    total_trip: int
    total_transaksi: int
    total_penumpang: int
    total_pendapatan: float
    kapasitas_tersedia: int
    okupansi_persen: float
    layanan_dominan: str
