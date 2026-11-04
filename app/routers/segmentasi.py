from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session
from app.core.config import MIN_BARIS_ANALISIS
from app.core.database import get_db
from app.core.deps import get_cabang_scope, require_roles
from app.models.models import Pengguna, Transaksi
from app.services import segmentasi_service as svc

router = APIRouter(prefix="/api/segmentasi", tags=["Segmentasi Pasar"])


@router.get("/ringkasan")
def ringkasan(db: Session = Depends(get_db), scope: str | None = Depends(get_cabang_scope)):
    """3 kartu ringkasan per Jenis Member (Mahasiswa/Umum/Non-Member) - sesuai UI Segmentasi.
    Kepala Outlet hanya melihat transaksi dari cabangnya."""
    return svc.ringkasan_per_member(db, cabang=scope)


@router.get("/cluster")
def cluster(n_cluster: int | None = Query(None, ge=svc.K_MIN, le=svc.K_MAX,
                                          description="Jumlah cluster; kosong = pilih otomatis (Silhouette tertinggi)"),
            cabang: str | None = Query(None, description="Opsional: batasi ke cabang asal tertentu"),
            limit: int = Query(5000, ge=1, le=20000),
            db: Session = Depends(get_db), _user: Pengguna = Depends(require_roles("Admin"))):
    """Jalankan RFM + Min-Max + K-Means (evaluasi Elbow, Silhouette, DBI untuk K = 2..8).
    Mengembalikan ringkasan tiap cluster, tabel pelanggan (Status Loyalitas) dan evaluasi."""
    total_baris = db.query(func.count(Transaksi.id_transaksi)).scalar() or 0
    if total_baris < MIN_BARIS_ANALISIS:
        raise HTTPException(
            status_code=400,
            detail=(f"Data transaksi baru {total_baris:,} baris; segmentasi membutuhkan minimal "
                    f"{MIN_BARIS_ANALISIS:,} baris.").replace(",", "."),
        )
    return svc.jalankan_rfm_kmeans(db, n_clusters=n_cluster, cabang=cabang or None, limit_pelanggan=limit)
