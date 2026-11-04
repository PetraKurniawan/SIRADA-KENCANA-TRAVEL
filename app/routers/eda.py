from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_cabang_scope
from starlette.concurrency import run_in_threadpool
from app.services import eda_service as svc
from app.services import reduksi_atribut_service as reduksi

router = APIRouter(prefix="/api/eda", tags=["Eksplorasi Data (EDA)"])


@router.get("")
def eda(tanggal_mulai: date | None = Query(None, description="Batas awal tanggal (YYYY-MM-DD)"),
        tanggal_selesai: date | None = Query(None, description="Batas akhir tanggal (YYYY-MM-DD)"),
        db: Session = Depends(get_db), scope: str | None = Depends(get_cabang_scope)):
    """Exploratory Data Analysis: ringkasan dataset, kelengkapan data, statistik deskriptif,
    tren bulanan, pola hari, efek kalender (akhir pekan/libur) dan distribusi kategori.
    Semua role yang login boleh mengakses; Kepala Outlet otomatis dibatasi ke cabangnya."""
    if tanggal_mulai and tanggal_selesai and tanggal_mulai > tanggal_selesai:
        raise HTTPException(status_code=400, detail="Tanggal mulai tidak boleh setelah tanggal selesai.")
    return svc.analisis_eda(db, cabang=scope, tanggal_mulai=tanggal_mulai, tanggal_selesai=tanggal_selesai)


@router.get("/reduksi-atribut")
async def reduksi_atribut(db: Session = Depends(get_db), scope: str | None = Depends(get_cabang_scope)):
    """Reduksi atribut berbasis algoritma: Variance Threshold + Missing Filter, korelasi
    (Pearson / Cramer's V), Mutual Information terhadap total_bayar, dan PCA. Mengembalikan
    keputusan per atribut beserta alasannya."""
    return await run_in_threadpool(reduksi.reduksi_atribut, db, scope)
