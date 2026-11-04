import logging
import os
import threading
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core import config
from app.core.migrate import jalankan_migrasi
from app.routers import (auth, dashboard, transaksi, segmentasi, forecasting, performa, pengguna,
                         eda, master, laporan, data_import, integrasi, tindak_lanjut)

jalankan_migrasi()

app = FastAPI(
    title="Kencana Analytics API",
    description="Backend untuk sistem analisis Big Data Kencana Travel — "
                 "segmentasi pasar, forecasting demand, dan analisis performa rute.",
    version="1.1.0",
)

# Hanya frontend yang boleh memanggil API dari browser. CORS_ORIGINS (dipisah koma) bisa
# dipakai untuk menambah origin lain; default-nya FRONTEND_URL + Vite dev server lokal.
_origins_env = os.getenv("CORS_ORIGINS", "")
ALLOWED_ORIGINS = ([o.strip().rstrip("/") for o in _origins_env.split(",") if o.strip()]
                   or [config.FRONTEND_URL, "http://localhost:5173", "http://127.0.0.1:5173"])

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(transaksi.router)
app.include_router(segmentasi.router)
app.include_router(forecasting.router)
app.include_router(performa.router)
app.include_router(pengguna.router)
app.include_router(eda.router)
app.include_router(master.router)
app.include_router(laporan.router)
app.include_router(data_import.router)
app.include_router(integrasi.router)
app.include_router(tindak_lanjut.router)


def _panaskan_cache():
    """Jalankan analisis berat sekali di thread latar setelah server hidup, supaya halaman
    Performa/Segmentasi/Forecasting dan laporan ringkasan tidak menunggu lama saat pertama
    dibuka. Hasilnya disimpan di cache memori masing-masing service. Aman bila gagal."""
    from app.core.database import SessionLocal
    from app.models.models import Rute
    from app.services import forecasting_service, integrasi_service, performa_service, segmentasi_service

    log = logging.getLogger("warmup")
    performa_service.panaskan_cache()
    db = SessionLocal()
    try:
        segmentasi_service.jalankan_rfm_kmeans(db)
        for (nama_rute,) in db.query(Rute.nama_rute).all():
            forecasting_service.jalankan_forecast(db, nama_rute, horizon_hari=30, pakai_kalender=True)
        integrasi_service.analisis_terpadu(db)
        log.info("Cache analisis siap.")
    except Exception:  # noqa: BLE001 - hanya optimasi
        log.exception("Pemanasan cache gagal (tidak memengaruhi API).")
    finally:
        db.close()


@app.on_event("startup")
def _mulai_pemanasan():
    if os.getenv("PANASKAN_CACHE", "1") == "1":
        threading.Thread(target=_panaskan_cache, daemon=True).start()


@app.get("/")
def root():
    return {"status": "ok", "service": "Kencana Analytics API", "docs": "/docs"}
