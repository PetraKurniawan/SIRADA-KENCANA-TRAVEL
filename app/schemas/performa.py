"""Skema respons Analisis Performa Rute & Cabang."""
from typing import Optional

from pydantic import BaseModel


class _MetrikPerforma(BaseModel):
    total_trip: int
    total_transaksi: int
    total_penumpang: int
    total_pendapatan: float
    kapasitas_tersedia: int
    okupansi_persen: float               # rata-rata okupansi per trip
    layanan_dominan: str
    layanan_dominan_persen: float
    rata_penumpang_per_trip: float
    status: str                          # Tinggi | Sedang | Rendah
    kontribusi_pendapatan_persen: float


class PerformaRuteOut(_MetrikPerforma):
    id_rute: str
    rute: str
    cabang_asal: Optional[str] = None
    cabang_tujuan: Optional[str] = None


class PerformaCabangOut(_MetrikPerforma):
    cabang: str
    jumlah_rute: int


class VipVsRegulerOut(BaseModel):
    layanan: str
    share_persen: float
    rata_harga_tiket: float
    total_pendapatan: float
    total_penumpang: int
    total_trip: int
    okupansi_persen: float
