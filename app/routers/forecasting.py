from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session
from app.core.config import MIN_BARIS_ANALISIS
from app.core.database import get_db
from app.core.deps import get_cabang_scope, require_roles
from app.models.models import Pengguna, Rute, Transaksi
from app.services import forecasting_service as svc
from app.schemas.forecasting import ForecastingRunRequest

router = APIRouter(prefix="/api/forecasting", tags=["Forecasting Demand"])


@router.get("/rute-tersedia")
def rute_tersedia(db: Session = Depends(get_db), cabang: str | None = Depends(get_cabang_scope)):
    """Untuk dropdown 'Koridor Perjalanan' di UI Forecasting.
    Kepala Outlet hanya melihat rute yang berangkat dari cabangnya."""
    q = db.query(Rute.nama_rute)
    if cabang is not None:
        q = q.filter(Rute.cabang_asal == cabang)
    return [r for (r,) in q.order_by(Rute.nama_rute).all()]


@router.post("/run")
def run(payload: ForecastingRunRequest, db: Session = Depends(get_db),
        _user: Pengguna = Depends(require_roles("Admin"))):
    """Tombol 'Jalankan Model': bandingkan ARIMA, SARIMA, SARIMAX (kalender) dan
    Holt-Winters untuk 1 rute, lalu pakai model dengan MAPE terkecil."""
    total = db.query(func.count(Transaksi.id_transaksi)).scalar() or 0
    if total < MIN_BARIS_ANALISIS:
        raise HTTPException(
            status_code=400,
            detail=f"Data transaksi baru {total:,} baris; forecasting membutuhkan minimal "
                   f"{MIN_BARIS_ANALISIS:,} baris.".replace(",", "."))
    try:
        return svc.jalankan_forecast(db, payload.rute, payload.horizon_hari, payload.pakai_kalender)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
