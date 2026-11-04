"""Skema respons fitur Import CSV + data cleaning."""
from datetime import date, datetime
from typing import List, Optional

from pydantic import BaseModel


class SampelDitolak(BaseModel):
    baris: int
    kode_transaksi: Optional[str] = None
    kategori: str  # kosong | tidak_valid | duplikat | sudah_ada
    alasan: str


class EntitasBaru(BaseModel):
    cabang: int = 0
    rute: int = 0
    armada: int = 0
    member: int = 0
    jenis_paket: int = 0


class HasilImport(BaseModel):
    dry_run: bool
    nama_file: str
    status: str
    catatan: Optional[str] = None
    baris_sumber: int
    baris_kosong_penuh: int
    baris_kosong: int
    baris_tidak_valid: int
    baris_duplikat: int
    baris_sudah_ada: int
    baris_lolos: int
    baris_dimuat: int
    entitas_baru: EntitasBaru
    sampel_ditolak: List[SampelDitolak]
    total_data_setelah: int
    min_baris_analisis: int
    siap_dianalisis: bool


class RiwayatImportOut(BaseModel):
    id_import: str
    nama_file: str
    waktu: datetime
    diimport_oleh: Optional[str] = None
    baris_sumber: int
    baris_kosong: int
    baris_tidak_valid: int
    baris_duplikat: int
    baris_sudah_ada: int
    baris_dimuat: int
    status: str
    catatan: Optional[str] = None


class StatusData(BaseModel):
    total_baris: int
    tanggal_awal: Optional[date] = None
    tanggal_akhir: Optional[date] = None
    min_baris_analisis: int
    siap_dianalisis: bool
    import_terakhir: Optional[RiwayatImportOut] = None
