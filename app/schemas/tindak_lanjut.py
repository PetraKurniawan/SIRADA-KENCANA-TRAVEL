"""Schema Pydantic untuk endpoint /api/tindak-lanjut."""
from datetime import date, datetime
from typing import Optional, Literal
from pydantic import BaseModel, Field

StatusTL = Literal["baru", "diproses", "selesai", "ditolak"]
PrioritasTL = Literal["tinggi", "sedang", "rendah"]


class TindakLanjutIn(BaseModel):
    id_rute: Optional[str] = None
    sumber: Optional[str] = None
    judul: str = Field(min_length=3, max_length=200)
    deskripsi: Optional[str] = None
    prioritas: PrioritasTL = "sedang"
    status: StatusTL = "baru"
    catatan: Optional[str] = None
    tenggat: Optional[date] = None


class TindakLanjutUpdate(BaseModel):
    judul: Optional[str] = Field(None, min_length=3, max_length=200)
    deskripsi: Optional[str] = None
    prioritas: Optional[PrioritasTL] = None
    status: Optional[StatusTL] = None
    catatan: Optional[str] = None
    tenggat: Optional[date] = None


class PembuatOut(BaseModel):
    id_pengguna: str
    nama: str
    role: str

    class Config:
        from_attributes = True


class RuteOut(BaseModel):
    id_rute: str
    nama_rute: str
    cabang_asal: str

    class Config:
        from_attributes = True


class TindakLanjutOut(BaseModel):
    id: str
    id_rute: Optional[str] = None
    sumber: Optional[str] = None
    judul: str
    deskripsi: Optional[str] = None
    prioritas: str
    status: str
    catatan: Optional[str] = None
    tenggat: Optional[date] = None
    dibuat_pada: datetime
    selesai_pada: Optional[datetime] = None
    pembuat: Optional[PembuatOut] = None
    rute: Optional[RuteOut] = None

    class Config:
        from_attributes = True
