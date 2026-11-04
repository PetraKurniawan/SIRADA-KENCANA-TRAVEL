"""Laporan analisis (PDF/Excel) dan alur permintaan laporan."""
from datetime import date
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session, aliased
from starlette.concurrency import run_in_threadpool

from app.core.database import get_db
from app.core.deps import cabang_scope, get_current_user, require_roles
from app.models.models import Cabang, Pengguna, PermintaanLaporan, utcnow
from app.schemas.laporan import JenisLaporan, PermintaanCreate, PermintaanOut, PermintaanProses
from app.services import laporan_service as svc

router = APIRouter(prefix="/api/laporan", tags=["Laporan"])

FormatLaporan = Literal["pdf", "xlsx"]


def _tentukan_cabang(db: Session, user: Pengguna, cabang: Optional[str]) -> Optional[str]:
    """Kepala Outlet selalu dikunci ke cabangnya; role lain boleh memilih (atau semua)."""
    scope = cabang_scope(user, db)
    if scope == "__tanpa_cabang__":
        raise HTTPException(403, "Akun Kepala Outlet belum terhubung ke cabang")
    if scope:
        return scope
    cabang = (cabang or "").strip() or None
    if cabang and not db.scalar(select(Cabang.nama_cabang).where(Cabang.nama_cabang == cabang)):
        raise HTTPException(400, f"Cabang '{cabang}' tidak ditemukan")
    return cabang


def _cek_periode(mulai: Optional[date], selesai: Optional[date]):
    if mulai and selesai and mulai > selesai:
        raise HTTPException(400, "Tanggal mulai tidak boleh setelah tanggal selesai")


async def _kirim_file(db, jenis, fmt, mulai, selesai, cabang, oleh: Pengguna) -> Response:
    # Analisis (terutama forecasting semua rute) berat & sinkron: jalankan di thread pool.
    isi, nama, media = await run_in_threadpool(
        svc.buat_file, db, jenis, fmt, mulai, selesai, cabang, f"{oleh.nama} ({oleh.role})")
    return Response(content=isi, media_type=media, headers={
        "Content-Disposition": f'attachment; filename="{nama}"',
        "Access-Control-Expose-Headers": "Content-Disposition",
    })


@router.get("/opsi")
def opsi_laporan(db: Session = Depends(get_db), user: Pengguna = Depends(get_current_user)):
    """Pilihan form laporan: daftar cabang dan cabang yang dikunci (Kepala Outlet)."""
    scope = cabang_scope(user, db)
    if scope:
        return {"cabang": [] if scope == "__tanpa_cabang__" else [scope],
                "cabang_terkunci": None if scope == "__tanpa_cabang__" else scope,
                "jenis": [{"value": k, "label": v} for k, v in svc.JUDUL_JENIS.items()]}
    return {"cabang": list(db.scalars(select(Cabang.nama_cabang).order_by(Cabang.nama_cabang))),
            "cabang_terkunci": None, "jenis": [{"value": k, "label": v} for k, v in svc.JUDUL_JENIS.items()]}


@router.get("/unduh")
async def unduh_laporan(
    jenis: JenisLaporan = Query("ringkasan"),
    format: FormatLaporan = Query("pdf"),
    tanggal_mulai: Optional[date] = Query(None),
    tanggal_selesai: Optional[date] = Query(None),
    cabang: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user: Pengguna = Depends(require_roles("Admin", "Owner", "KepalaOutlet")),
):
    _cek_periode(tanggal_mulai, tanggal_selesai)
    cabang = _tentukan_cabang(db, user, cabang)
    return await _kirim_file(db, jenis, format, tanggal_mulai, tanggal_selesai, cabang, user)


def _ke_out(p: PermintaanLaporan, pemohon: Optional[Pengguna],
            pemroses: Optional[Pengguna]) -> PermintaanOut:
    return PermintaanOut(
        id_permintaan=p.id_permintaan, id_pemohon=p.id_pemohon,
        nama_pemohon=pemohon.nama if pemohon else None,
        role_pemohon=pemohon.role if pemohon else None,
        jenis_laporan=p.jenis_laporan, tanggal_mulai=p.tanggal_mulai,
        tanggal_selesai=p.tanggal_selesai, cabang=p.cabang, catatan=p.catatan, status=p.status,
        catatan_admin=p.catatan_admin, diproses_oleh=p.diproses_oleh,
        nama_pemroses=pemroses.nama if pemroses else None,
        dibuat_pada=p.dibuat_pada, diproses_pada=p.diproses_pada,
    )


@router.post("/permintaan", response_model=PermintaanOut)
def ajukan_permintaan(payload: PermintaanCreate, db: Session = Depends(get_db),
                      user: Pengguna = Depends(require_roles("Owner", "KepalaOutlet"))):
    cabang = _tentukan_cabang(db, user, payload.cabang)
    p = PermintaanLaporan(
        id_pemohon=user.id_pengguna, jenis_laporan=payload.jenis_laporan,
        tanggal_mulai=payload.tanggal_mulai, tanggal_selesai=payload.tanggal_selesai,
        cabang=cabang, catatan=(payload.catatan or "").strip() or None, status="menunggu",
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return _ke_out(p, user, None)


@router.get("/permintaan", response_model=List[PermintaanOut])
def daftar_permintaan(status: Optional[Literal["menunggu", "selesai", "ditolak"]] = Query(None),
                      db: Session = Depends(get_db),
                      user: Pengguna = Depends(get_current_user)):
    """Admin melihat semua permintaan; Owner/Kepala Outlet hanya permintaannya sendiri."""
    pemohon, pemroses = aliased(Pengguna), aliased(Pengguna)
    q = (select(PermintaanLaporan, pemohon, pemroses)
         .outerjoin(pemohon, pemohon.id_pengguna == PermintaanLaporan.id_pemohon)
         .outerjoin(pemroses, pemroses.id_pengguna == PermintaanLaporan.diproses_oleh)
         .order_by(PermintaanLaporan.dibuat_pada.desc()))
    if user.role != "Admin":
        q = q.where(PermintaanLaporan.id_pemohon == user.id_pengguna)
    if status:
        q = q.where(PermintaanLaporan.status == status)
    return [_ke_out(p, a, b) for p, a, b in db.execute(q).all()]


@router.post("/permintaan/{id_permintaan}/proses", response_model=PermintaanOut)
def proses_permintaan(id_permintaan: str, payload: PermintaanProses,
                      db: Session = Depends(get_db),
                      admin: Pengguna = Depends(require_roles("Admin"))):
    p = db.get(PermintaanLaporan, id_permintaan)
    if not p:
        raise HTTPException(404, "Permintaan tidak ditemukan")
    if p.status != "menunggu":
        raise HTTPException(400, f"Permintaan sudah diproses (status: {p.status})")
    catatan = (payload.catatan_admin or "").strip() or None
    if payload.status == "ditolak" and not catatan:
        raise HTTPException(400, "Isi alasan penolakan di catatan admin")
    p.status = payload.status
    p.catatan_admin = catatan
    p.diproses_oleh = admin.id_pengguna
    p.diproses_pada = utcnow()
    db.commit()
    db.refresh(p)
    return _ke_out(p, db.get(Pengguna, p.id_pemohon), admin)


@router.get("/permintaan/{id_permintaan}/unduh")
async def unduh_permintaan(id_permintaan: str, format: FormatLaporan = Query("pdf"),
                           db: Session = Depends(get_db),
                           user: Pengguna = Depends(get_current_user)):
    p = db.get(PermintaanLaporan, id_permintaan)
    if not p:
        raise HTTPException(404, "Permintaan tidak ditemukan")
    if user.role != "Admin" and p.id_pemohon != user.id_pengguna:
        raise HTTPException(403, "Tidak punya akses ke permintaan ini")
    if p.status != "selesai":
        raise HTTPException(400, "Laporan baru bisa diunduh setelah permintaan diselesaikan Admin")
    return await _kirim_file(db, p.jenis_laporan, format, p.tanggal_mulai, p.tanggal_selesai,
                             p.cabang, user)
