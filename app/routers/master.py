"""
Data Master: Rute, Armada, Jadwal (CRUD) dan Cabang (baca saja).
- Admin   : kelola semua data.
- KepalaOutlet : hanya data cabangnya (rute & jadwal yang berangkat dari cabangnya, armada
                 yang berbasis di cabangnya). Daftar juga dibatasi ke cabangnya.
- Owner   : hanya melihat.
"""
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import cabang_scope, get_cabang_scope, require_roles
from app.models.models import Pengguna
from app.schemas.master import (ArmadaIn, ArmadaOut, CabangOut, JadwalIn, JadwalOut, RuteIn,
                                RuteOut)
from app.schemas.schemas import PesanResponse
from app.services import master_service as svc

router = APIRouter(prefix="/api/master", tags=["Data Master"])

_pengelola = require_roles("Admin", "KepalaOutlet")


def _scope_pengelola(user: Pengguna = Depends(_pengelola), db: Session = Depends(get_db)) -> Optional[str]:
    return cabang_scope(user, db)


# ---------------------------------------------------------------- cabang
@router.get("/cabang", response_model=list[CabangOut])
def daftar_cabang(db: Session = Depends(get_db), scope: Optional[str] = Depends(get_cabang_scope)):
    return svc.list_cabang(db, scope)


# ---------------------------------------------------------------- rute
@router.get("/rute", response_model=list[RuteOut])
def daftar_rute(db: Session = Depends(get_db), scope: Optional[str] = Depends(get_cabang_scope)):
    return svc.list_rute(db, scope)


@router.post("/rute", response_model=RuteOut, status_code=201)
def tambah_rute(data: RuteIn, db: Session = Depends(get_db),
                scope: Optional[str] = Depends(_scope_pengelola)):
    return svc.create_rute(db, data, scope)


@router.put("/rute/{id_rute}", response_model=RuteOut)
def ubah_rute(id_rute: str, data: RuteIn, db: Session = Depends(get_db),
              scope: Optional[str] = Depends(_scope_pengelola)):
    return svc.update_rute(db, id_rute, data, scope)


@router.delete("/rute/{id_rute}", response_model=PesanResponse)
def hapus_rute(id_rute: str, db: Session = Depends(get_db),
               scope: Optional[str] = Depends(_scope_pengelola)):
    return svc.delete_rute(db, id_rute, scope)


# ---------------------------------------------------------------- armada
@router.get("/armada", response_model=list[ArmadaOut])
def daftar_armada(db: Session = Depends(get_db), scope: Optional[str] = Depends(get_cabang_scope)):
    return svc.list_armada(db, scope)


@router.post("/armada", response_model=ArmadaOut, status_code=201)
def tambah_armada(data: ArmadaIn, db: Session = Depends(get_db),
                  scope: Optional[str] = Depends(_scope_pengelola)):
    return svc.create_armada(db, data, scope)


@router.put("/armada/{id_armada}", response_model=ArmadaOut)
def ubah_armada(id_armada: str, data: ArmadaIn, db: Session = Depends(get_db),
                scope: Optional[str] = Depends(_scope_pengelola)):
    return svc.update_armada(db, id_armada, data, scope)


@router.delete("/armada/{id_armada}", response_model=PesanResponse)
def hapus_armada(id_armada: str, db: Session = Depends(get_db),
                 scope: Optional[str] = Depends(_scope_pengelola)):
    return svc.delete_armada(db, id_armada, scope)


# ---------------------------------------------------------------- jadwal
@router.get("/jadwal", response_model=list[JadwalOut])
def daftar_jadwal(id_rute: Optional[str] = Query(None), db: Session = Depends(get_db),
                  scope: Optional[str] = Depends(get_cabang_scope)):
    return svc.list_jadwal(db, scope, id_rute)


@router.post("/jadwal", response_model=JadwalOut, status_code=201)
def tambah_jadwal(data: JadwalIn, db: Session = Depends(get_db),
                  scope: Optional[str] = Depends(_scope_pengelola)):
    return svc.create_jadwal(db, data, scope)


@router.put("/jadwal/{id_jadwal}", response_model=JadwalOut)
def ubah_jadwal(id_jadwal: str, data: JadwalIn, db: Session = Depends(get_db),
                scope: Optional[str] = Depends(_scope_pengelola)):
    return svc.update_jadwal(db, id_jadwal, data, scope)


@router.delete("/jadwal/{id_jadwal}", response_model=PesanResponse)
def hapus_jadwal(id_jadwal: str, db: Session = Depends(get_db),
                 scope: Optional[str] = Depends(_scope_pengelola)):
    return svc.delete_jadwal(db, id_jadwal, scope)
