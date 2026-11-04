"""Skema Data Master: Rute, Armada, Jadwal, Cabang."""
import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

Layanan = Literal["VIP", "Reguler"]
_JAM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _bersih(v: str) -> str:
    v = (v or "").strip()
    if not v:
        raise ValueError("Wajib diisi")
    return v


def _layanan_list(v) -> str:
    """Terima "VIP", "VIP,Reguler" atau ["VIP","Reguler"] -> string dipisah koma, urut & unik."""
    if isinstance(v, str):
        bagian = [x.strip() for x in v.split(",")]
    else:
        bagian = [str(x).strip() for x in v]
    bagian = [x for x in bagian if x]
    if not bagian:
        raise ValueError("Pilih minimal satu layanan")
    salah = [x for x in bagian if x not in ("VIP", "Reguler")]
    if salah:
        raise ValueError(f"Layanan tidak dikenal: {', '.join(salah)} (hanya VIP / Reguler)")
    return ",".join(sorted(set(bagian), key=["Reguler", "VIP"].index))


# ---------- CABANG ----------
class CabangOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id_cabang: str
    nama_cabang: str
    kota: str
    jumlah_rute: int = 0
    jumlah_armada: int = 0
    cabang_saya: bool = False  # True = cabang Kepala Outlet yang sedang login


# ---------- RUTE ----------
class RuteIn(BaseModel):
    cabang_asal: str
    cabang_tujuan: str
    layanan_tersedia: str  # "VIP" | "Reguler" | "Reguler,VIP"

    @field_validator("cabang_asal", "cabang_tujuan")
    @classmethod
    def _cabang(cls, v: str) -> str:
        return _bersih(v)

    @field_validator("layanan_tersedia", mode="before")
    @classmethod
    def _layanan(cls, v):
        return _layanan_list(v)


class RuteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id_rute: str
    nama_rute: str
    cabang_asal: str
    cabang_tujuan: str
    layanan_tersedia: str
    jumlah_jadwal: int = 0
    jumlah_transaksi: int = 0


# ---------- ARMADA ----------
class ArmadaIn(BaseModel):
    kode_armada: str = Field(max_length=30)
    jenis_kendaraan: str = Field(max_length=60)
    tipe_layanan: Layanan
    kapasitas: int = Field(gt=0, le=60, description="Jumlah kursi penumpang")
    basis_outlet: str

    @field_validator("kode_armada", "jenis_kendaraan", "basis_outlet")
    @classmethod
    def _wajib(cls, v: str) -> str:
        return _bersih(v)

    @field_validator("kode_armada")
    @classmethod
    def _kode(cls, v: str) -> str:
        return v.upper()


class ArmadaOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id_armada: str
    kode_armada: str
    jenis_kendaraan: str
    tipe_layanan: str
    kapasitas: int
    basis_outlet: Optional[str] = None


# ---------- JADWAL ----------
class JadwalIn(BaseModel):
    id_rute: str
    jam_keberangkatan: str
    layanan: Layanan
    hari_berlaku: str = "Setiap Hari"

    @field_validator("jam_keberangkatan")
    @classmethod
    def _jam(cls, v: str) -> str:
        v = (v or "").strip()[:5]
        if not _JAM_RE.match(v):
            raise ValueError("Jam keberangkatan harus berformat HH:MM (00:00-23:59)")
        return v

    @field_validator("hari_berlaku")
    @classmethod
    def _hari(cls, v: str) -> str:
        return (v or "").strip() or "Setiap Hari"


class JadwalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id_jadwal: str
    id_rute: str
    nama_rute: str
    cabang_asal: str
    jam_keberangkatan: str
    layanan: str
    hari_berlaku: Optional[str] = None
