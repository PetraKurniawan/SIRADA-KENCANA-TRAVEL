from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_cabang_scope
from app.schemas.performa import PerformaCabangOut, PerformaRuteOut, VipVsRegulerOut
from app.services import performa_service as svc
from app.services import performa_ml_service as ml_svc

router = APIRouter(prefix="/api/performa-rute", tags=["Performa Rute"])


def _cek_rentang(mulai: Optional[date], selesai: Optional[date]):
    if mulai and selesai and mulai > selesai:
        raise HTTPException(status_code=422, detail="Tanggal mulai harus sebelum tanggal selesai")


@router.get("", response_model=list[PerformaRuteOut])
def performa(tanggal_mulai: Optional[date] = Query(None),
             tanggal_selesai: Optional[date] = Query(None),
             db: Session = Depends(get_db), scope: Optional[str] = Depends(get_cabang_scope)):
    """Tabel 'Detail Performa Rute' + distribusi volume di UI Performa Rute.
    Kepala Outlet hanya melihat rute yang berangkat dari cabangnya."""
    _cek_rentang(tanggal_mulai, tanggal_selesai)
    return svc.analisis_performa_rute(db, scope, tanggal_mulai, tanggal_selesai)


@router.get("/cabang", response_model=list[PerformaCabangOut])
def performa_cabang(tanggal_mulai: Optional[date] = Query(None),
                    tanggal_selesai: Optional[date] = Query(None),
                    db: Session = Depends(get_db), scope: Optional[str] = Depends(get_cabang_scope)):
    """Performa per cabang asal keberangkatan."""
    _cek_rentang(tanggal_mulai, tanggal_selesai)
    return svc.analisis_performa_cabang(db, scope, tanggal_mulai, tanggal_selesai)


@router.get("/vip-vs-reguler", response_model=list[VipVsRegulerOut])
def vip_vs_reguler(tanggal_mulai: Optional[date] = Query(None),
                   tanggal_selesai: Optional[date] = Query(None),
                   db: Session = Depends(get_db), scope: Optional[str] = Depends(get_cabang_scope)):
    """Sesuai kartu 'Perbandingan Layanan VIP vs Reguler' di UI."""
    _cek_rentang(tanggal_mulai, tanggal_selesai)
    return svc.perbandingan_vip_reguler(db, scope, tanggal_mulai, tanggal_selesai)


@router.get("/ml")
def performa_ml(db: Session = Depends(get_db), scope: Optional[str] = Depends(get_cabang_scope)):
    """Analisis performa rute berbasis ML: metrik model, faktor penting, slot bermasalah,
    dan prediksi okupansi 30 hari ke depan. Akses sesuai cabang scope."""
    return ml_svc.analisis_performa_ml(db, cabang=scope)
