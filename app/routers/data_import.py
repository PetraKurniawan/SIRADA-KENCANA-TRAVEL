"""Import CSV transaksi dengan data cleaning (khusus Admin)."""
from typing import List

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.core.database import get_db
from app.core.deps import require_roles
from app.models.models import Pengguna
from app.schemas.import_data import HasilImport, RiwayatImportOut, StatusData
from app.services import import_service as svc

router = APIRouter(prefix="/api/import", tags=["Import Data"])


@router.post("/transaksi", response_model=HasilImport)
async def import_transaksi(
    file: UploadFile = File(..., description="File CSV transaksi (maks. 50 MB)"),
    dry_run: bool = Query(False, description="True = validasi & cleaning saja, tanpa menyimpan"),
    db: Session = Depends(get_db),
    user: Pengguna = Depends(require_roles("Admin")),
):
    nama = file.filename or "upload.csv"
    if not nama.lower().endswith(".csv"):
        raise HTTPException(400, "File harus berformat .csv")
    isi = await file.read(svc.MAKS_UKURAN_FILE + 1)
    if len(isi) > svc.MAKS_UKURAN_FILE:
        raise HTTPException(413, "Ukuran file melebihi 50 MB")
    try:
        # Fungsi sinkron & berat: jalankan di thread pool agar event loop tidak terblokir.
        return await run_in_threadpool(svc.import_transaksi, db, isi, nama, user, dry_run)
    except svc.ImportError400 as e:
        raise HTTPException(400, str(e)) from e


@router.get("/riwayat", response_model=List[RiwayatImportOut])
def riwayat_import(limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db),
                   _user: Pengguna = Depends(require_roles("Admin"))):
    return svc.daftar_riwayat(db, limit)


@router.get("/status-data", response_model=StatusData)
def status_data(db: Session = Depends(get_db),
                _user: Pengguna = Depends(require_roles("Admin", "Owner", "KepalaOutlet"))):
    return svc.status_data(db)


@router.get("/template")
def template(_user: Pengguna = Depends(require_roles("Admin"))):
    return Response(content=svc.template_csv(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="template_transaksi.csv"'})
