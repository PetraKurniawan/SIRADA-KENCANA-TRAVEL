"""Skema permintaan laporan (Owner/Kepala Outlet mengajukan, Admin memproses)."""
from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

JenisLaporan = Literal["ringkasan", "lengkap", "terpadu", "segmentasi", "forecasting", "performa"]


class PermintaanCreate(BaseModel):
    jenis_laporan: JenisLaporan
    tanggal_mulai: Optional[date] = None
    tanggal_selesai: Optional[date] = None
    cabang: Optional[str] = None
    catatan: Optional[str] = Field(None, max_length=500)

    @model_validator(mode="after")
    def _cek_periode(self):
        if self.tanggal_mulai and self.tanggal_selesai and self.tanggal_mulai > self.tanggal_selesai:
            raise ValueError("Tanggal mulai tidak boleh setelah tanggal selesai")
        return self


class PermintaanProses(BaseModel):
    status: Literal["selesai", "ditolak"]
    catatan_admin: Optional[str] = Field(None, max_length=500)


class PermintaanOut(BaseModel):
    id_permintaan: str
    id_pemohon: str
    nama_pemohon: Optional[str] = None
    role_pemohon: Optional[str] = None
    jenis_laporan: str
    tanggal_mulai: Optional[date] = None
    tanggal_selesai: Optional[date] = None
    cabang: Optional[str] = None
    catatan: Optional[str] = None
    status: str
    catatan_admin: Optional[str] = None
    diproses_oleh: Optional[str] = None
    nama_pemroses: Optional[str] = None
    dibuat_pada: datetime
    diproses_pada: Optional[datetime] = None
