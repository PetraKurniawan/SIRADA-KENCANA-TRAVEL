"""Analisis Terpadu: hubungan Segmentasi - Forecasting - Performa Rute."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.core.config import MIN_BARIS_ANALISIS
from app.core.database import get_db
from app.core.deps import get_cabang_scope
from app.models.models import Transaksi
from app.services import integrasi_service as svc

router = APIRouter(prefix="/api/integrasi", tags=["Analisis Terpadu"])


@router.get("")
async def analisis_terpadu(db: Session = Depends(get_db), scope: str | None = Depends(get_cabang_scope)):
    """Menggabungkan hasil segmentasi (RFM + K-Means), forecasting 30 hari dan performa rute
    (okupansi) menjadi satu analisis: komposisi segmen per rute, korelasi antar-modul
    (Pearson/Spearman pada panel rute x bulan) dan rekomendasi tindakan per rute.
    Semua role boleh melihat; Kepala Outlet dibatasi ke cabangnya. Hasil analisis berat
    di-cache, sehingga pemanggilan pertama setelah data berubah bisa memakan waktu."""
    total = db.query(func.count(Transaksi.id_transaksi)).scalar() or 0
    if total < MIN_BARIS_ANALISIS:
        raise HTTPException(
            status_code=400,
            detail=(f"Data transaksi baru {total:,} baris; analisis terpadu membutuhkan minimal "
                    f"{MIN_BARIS_ANALISIS:,} baris.").replace(",", "."))
    try:
        return await run_in_threadpool(svc.analisis_terpadu, db, scope)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
