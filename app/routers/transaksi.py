import io
import csv
import re
import uuid
from datetime import date
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.core.database import get_db
from app.core.deps import get_cabang_scope, require_roles
from app.models.models import Armada, Transaksi, Rute, Member, Pengguna
from app.schemas.schemas import TransaksiListOut, TransaksiOut, TransaksiCreate
from app.services.referensi_service import NAMA_HARI, pastikan_jadwal, pastikan_kalender

router = APIRouter(prefix="/api/transaksi", tags=["Data Transaksi"])

_JAM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class TransaksiListRingkasOut(TransaksiListOut):
    """Daftar transaksi + ringkasan untuk kartu di kanan atas halaman (mengikuti filter)."""
    total_pendapatan: float = 0


def _base_query(db: Session):
    return db.query(
        Transaksi.id_transaksi, Transaksi.tanggal, Transaksi.jam_keberangkatan,
        Rute.nama_rute.label("rute"), Transaksi.layanan, Transaksi.channel_pemesanan,
        Member.jenis_member.label("jenis_member"), Transaksi.jumlah_unit,
        Transaksi.satuan, Transaksi.total_bayar, Transaksi.keterangan,
    ).join(Rute, Rute.id_rute == Transaksi.id_rute
    ).outerjoin(Member, Member.id_member == Transaksi.id_member)


def _filter(q, scope, cari, rute, layanan, channel, tanggal_mulai, tanggal_selesai):
    if scope:  # Kepala Outlet hanya melihat transaksi dari cabangnya
        q = q.filter(Transaksi.cabang_asal == scope)
    if cari:
        like = f"%{cari}%"
        q = q.filter(Transaksi.id_transaksi.ilike(like) | Transaksi.id_pelanggan.ilike(like))
    if rute:
        q = q.filter(Rute.nama_rute == rute)
    if layanan:
        q = q.filter(Transaksi.layanan == layanan)
    if channel:
        q = q.filter(Transaksi.channel_pemesanan == channel)
    if tanggal_mulai:
        q = q.filter(Transaksi.tanggal >= tanggal_mulai)
    if tanggal_selesai:
        q = q.filter(Transaksi.tanggal <= tanggal_selesai)
    return q


@router.get("", response_model=TransaksiListRingkasOut)
def list_transaksi(
    db: Session = Depends(get_db),
    scope: Optional[str] = Depends(get_cabang_scope),
    cari: Optional[str] = Query(None, description="Cari kode transaksi / ID pelanggan"),
    rute: Optional[str] = Query(None),
    layanan: Optional[str] = Query(None),
    channel: Optional[str] = Query(None),
    tanggal_mulai: Optional[date] = Query(None),
    tanggal_selesai: Optional[date] = Query(None),
    halaman: int = Query(1, ge=1),
    per_halaman: int = Query(10, ge=1, le=200),
):
    """Sesuai UI 'Data Transaksi Pemesanan Tiket': search box, filter Rute/Layanan/Channel, pagination."""
    q = _filter(_base_query(db), scope, cari, rute, layanan, channel, tanggal_mulai, tanggal_selesai)

    # Hitung jumlah baris & total pendapatan dalam satu query (bukan dua full scan).
    total, total_pendapatan = q.order_by(None).with_entities(
        func.count(Transaksi.id_transaksi), func.coalesce(func.sum(Transaksi.total_bayar), 0)).one()
    rows = (q.order_by(Transaksi.tanggal.desc(), Transaksi.jam_keberangkatan.desc())
             .offset((halaman - 1) * per_halaman).limit(per_halaman).all())
    data = [TransaksiOut(**row._mapping) for row in rows]
    return TransaksiListRingkasOut(total=total, halaman=halaman, per_halaman=per_halaman, data=data,
                                   total_pendapatan=float(total_pendapatan or 0))


def _median_harga(db: Session, id_rute: Optional[str], layanan: Optional[str]) -> Optional[float]:
    """Median harga_satuan transaksi Penumpang (portabel SQLite/Postgres: ORDER BY + OFFSET)."""
    q = db.query(Transaksi.harga_satuan).filter(Transaksi.jenis_transaksi == "Penumpang",
                                                Transaksi.harga_satuan > 0)
    if id_rute:
        q = q.filter(Transaksi.id_rute == id_rute)
    if layanan:
        q = q.filter(Transaksi.layanan == layanan)
    n = q.order_by(None).count()
    if n == 0:
        return None
    ordered = q.order_by(Transaksi.harga_satuan)
    if n % 2:
        return float(ordered.offset(n // 2).limit(1).scalar())
    dua = [r[0] for r in ordered.offset(n // 2 - 1).limit(2).all()]
    return float(sum(dua) / 2)


@router.post("", response_model=TransaksiOut)
def tambah_transaksi(payload: TransaksiCreate, db: Session = Depends(get_db),
                     user: Pengguna = Depends(require_roles("Admin"))):
    """Sesuai tombol 'Tambah Transaksi' di UI."""
    jam = payload.jam_keberangkatan.strip()[:5]
    if not _JAM_RE.match(jam):
        raise HTTPException(status_code=422, detail="Jam keberangkatan harus berformat HH:MM")
    if payload.layanan not in ("VIP", "Reguler"):
        raise HTTPException(status_code=422, detail="Layanan harus VIP atau Reguler")
    if payload.jumlah_unit <= 0:
        raise HTTPException(status_code=422, detail="Jumlah unit harus lebih dari 0")

    rute = db.query(Rute).filter(Rute.nama_rute == payload.rute).first()
    if not rute:
        raise HTTPException(status_code=422,
                            detail=f"Rute '{payload.rute}' tidak ditemukan. Tambahkan dulu di Data Master.")
    tersedia = [x.strip() for x in (rute.layanan_tersedia or "").split(",") if x.strip()]
    if tersedia and payload.layanan not in tersedia:
        raise HTTPException(status_code=422,
                            detail=f"Layanan {payload.layanan} tidak tersedia di rute {rute.nama_rute} "
                                   f"(tersedia: {', '.join(tersedia)})")

    member = db.query(Member).filter(Member.jenis_member == payload.jenis_member).first()
    diskon = member.diskon_per_tiket if member else 0
    # Tarif = median harga tiket historis untuk rute + layanan ini (fallback: median seluruh data).
    harga_dasar = (_median_harga(db, rute.id_rute, payload.layanan)
                   or _median_harga(db, None, payload.layanan)
                   or _median_harga(db, None, None) or 0.0)
    total_harga = harga_dasar * payload.jumlah_unit
    total_diskon = min(diskon * payload.jumlah_unit, total_harga)

    # Armada: pakai armada yang sudah melayani trip yang sama, bila ada.
    id_armada = (db.query(Transaksi.id_armada)
                   .filter(Transaksi.id_rute == rute.id_rute, Transaksi.tanggal == payload.tanggal,
                           Transaksi.jam_keberangkatan == jam, Transaksi.layanan == payload.layanan,
                           Transaksi.id_armada.isnot(None))
                   .limit(1).scalar())
    if id_armada is None:
        id_armada = (db.query(Armada.id_armada)
                       .filter(Armada.basis_outlet == rute.cabang_asal,
                               Armada.tipe_layanan == payload.layanan)
                       .order_by(Armada.kode_armada).limit(1).scalar())

    # Wajib sebelum insert: FK transaksi -> kalender & transaksi -> jadwal (gagal di Postgres).
    pastikan_kalender(db, payload.tanggal, payload.tanggal)
    pastikan_jadwal(db, [(rute.id_rute, jam, payload.layanan)])

    kode = f"KCN{uuid.uuid4().hex[:8].upper()}"
    trx = Transaksi(
        id_transaksi=kode, tanggal=payload.tanggal, hari=NAMA_HARI[payload.tanggal.weekday()],
        jam_keberangkatan=jam, id_rute=rute.id_rute, id_armada=id_armada,
        cabang_asal=rute.cabang_asal, cabang_tujuan=rute.cabang_tujuan,
        layanan=payload.layanan, jenis_transaksi="Penumpang",
        id_member=member.id_member if member else None,
        jumlah_unit=payload.jumlah_unit, satuan="Orang",
        channel_pemesanan=payload.channel_pemesanan, harga_satuan=harga_dasar,
        diskon_per_tiket=diskon, total_harga=total_harga, total_diskon=total_diskon,
        total_bayar=total_harga - total_diskon, keterangan="Input Manual",
        id_pelanggan=(payload.id_pelanggan or "").strip() or None,
    )
    db.add(trx)
    db.commit()
    row = _base_query(db).filter(Transaksi.id_transaksi == kode).first()
    return TransaksiOut(**row._mapping)


@router.get("/export")
def export_csv(
    db: Session = Depends(get_db),
    scope: Optional[str] = Depends(get_cabang_scope),
    cari: Optional[str] = Query(None),
    rute: Optional[str] = Query(None),
    layanan: Optional[str] = Query(None),
    channel: Optional[str] = Query(None),
    tanggal_mulai: Optional[date] = Query(None),
    tanggal_selesai: Optional[date] = Query(None),
):
    """Export CSV transaksi sesuai filter yang sedang aktif (maks. 50.000 baris terbaru)."""
    q = _filter(_base_query(db), scope, cari, rute, layanan, channel, tanggal_mulai, tanggal_selesai)
    rows = q.order_by(Transaksi.tanggal.desc(), Transaksi.jam_keberangkatan.desc()).limit(50000).all()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Kode Transaksi", "Tanggal", "Jam", "Rute", "Layanan", "Channel",
                     "Jenis Member", "Jumlah Unit", "Satuan", "Total Bayar", "Keterangan"])
    for r in rows:
        writer.writerow(list(r._mapping.values()))
    buf.seek(0)
    nama = f"transaksi_{scope.lower()}.csv" if scope else "transaksi_export.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename={nama}"})
