from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.core.database import get_db
from app.core.deps import get_cabang_scope
from app.models.models import Transaksi, Rute, Member

router = APIRouter(prefix="/api/dashboard", tags=["Dashboard"])


def _scoped(q, scope: Optional[str]):
    """Kepala Outlet hanya melihat transaksi yang berangkat dari cabangnya."""
    return q.filter(Transaksi.cabang_asal == scope) if scope else q


@router.get("/summary")
def summary(db: Session = Depends(get_db), scope: Optional[str] = Depends(get_cabang_scope)):
    """4 kartu atas: Total Transaksi, Total Pendapatan, Rute Terlaris, Rasio Member/Non."""
    total_transaksi, total_pendapatan = _scoped(
        db.query(func.count(Transaksi.id_transaksi), func.sum(Transaksi.total_bayar)), scope).one()

    rute_terlaris = _scoped(
        db.query(Rute.nama_rute, func.sum(Transaksi.jumlah_unit).label("pnp"))
          .join(Transaksi, Transaksi.id_rute == Rute.id_rute)
          .filter(Transaksi.jenis_transaksi == "Penumpang"), scope
    ).group_by(Rute.nama_rute).order_by(func.sum(Transaksi.jumlah_unit).desc()).first()

    total_member = _scoped(
        db.query(func.count(Transaksi.id_transaksi))
          .join(Member, Member.id_member == Transaksi.id_member)
          .filter(Transaksi.jenis_transaksi == "Penumpang",
                  Member.jenis_member.notin_(["Non-Member", "-"])), scope).scalar() or 0
    total_penumpang_tx = _scoped(
        db.query(func.count(Transaksi.id_transaksi))
          .filter(Transaksi.jenis_transaksi == "Penumpang"), scope).scalar() or 0
    total_non_member = total_penumpang_tx - total_member
    grand = max(total_penumpang_tx, 1)

    return {
        "total_transaksi": total_transaksi or 0,
        "total_pendapatan": float(total_pendapatan or 0),
        "rute_terlaris": rute_terlaris.nama_rute if rute_terlaris else "-",
        "penumpang_rute_terlaris": int(rute_terlaris.pnp) if rute_terlaris else 0,
        "rasio_member_persen": round(100 * total_member / grand, 1),
        "rasio_non_member_persen": round(100 * total_non_member / grand, 1),
        "cabang": scope,
    }


@router.get("/distribusi-member")
def distribusi_member(db: Session = Depends(get_db), scope: Optional[str] = Depends(get_cabang_scope)):
    """Donut chart 'Segmentasi Pelanggan' di Dashboard."""
    rows = _scoped(
        db.query(Member.jenis_member, func.count(Transaksi.id_transaksi).label("jumlah"))
          .join(Transaksi, Transaksi.id_member == Member.id_member)
          .filter(Transaksi.jenis_transaksi == "Penumpang"), scope
    ).group_by(Member.jenis_member).all()
    total = sum(r.jumlah for r in rows) or 1
    return [{"jenis_member": r.jenis_member, "jumlah": r.jumlah,
             "persen": round(100 * r.jumlah / total, 1)} for r in rows]


@router.get("/aktivitas-terkini")
def aktivitas_terkini(limit: int = Query(5, ge=1, le=50), db: Session = Depends(get_db),
                      scope: Optional[str] = Depends(get_cabang_scope)):
    """Tabel 'Aktivitas Transaksi Terkini' di bagian bawah Dashboard."""
    rows = _scoped(
        db.query(Transaksi.id_transaksi, Transaksi.tanggal, Transaksi.id_pelanggan, Rute.nama_rute,
                 Transaksi.jam_keberangkatan, Transaksi.layanan, Transaksi.total_bayar,
                 Member.jenis_member)
          .join(Rute, Rute.id_rute == Transaksi.id_rute)
          .outerjoin(Member, Member.id_member == Transaksi.id_member), scope
    ).order_by(Transaksi.tanggal.desc(), Transaksi.jam_keberangkatan.desc()).limit(limit).all()
    return [dict(r._mapping) for r in rows]


@router.get("/tren-penumpang")
def tren_penumpang(hari: int = Query(14, ge=7, le=90), db: Session = Depends(get_db),
                   scope: Optional[str] = Depends(get_cabang_scope)):
    """Panel tren di Dashboard: jumlah penumpang & pendapatan per hari untuk N hari terakhir
    yang ada datanya (data aktual, bukan proyeksi)."""
    terakhir = _scoped(db.query(func.max(Transaksi.tanggal)), scope).scalar()
    if not terakhir:
        return []
    mulai = terakhir - timedelta(days=hari - 1)
    rows = _scoped(
        db.query(Transaksi.tanggal,
                 func.coalesce(func.sum(Transaksi.jumlah_unit), 0).label("penumpang"),
                 func.count(Transaksi.id_transaksi).label("transaksi"),
                 func.coalesce(func.sum(Transaksi.total_bayar), 0).label("pendapatan"))
          .filter(Transaksi.jenis_transaksi == "Penumpang",
                  Transaksi.tanggal >= mulai, Transaksi.tanggal <= terakhir), scope
    ).group_by(Transaksi.tanggal).order_by(Transaksi.tanggal).all()
    return [{"tanggal": r.tanggal, "penumpang": int(r.penumpang), "transaksi": r.transaksi,
             "pendapatan": float(r.pendapatan)} for r in rows]
