"""
Import CSV transaksi + data cleaning.

Alur cleaning (setiap langkah dihitung dan dicatat di tabel riwayat_import):
  1. Cek kolom wajib ada di header (kalau kurang -> ditolak seluruhnya).
  2. Trim spasi tiap sel, buang baris yang seluruhnya kosong.
  3. Buang baris dengan kolom wajib kosong                      -> baris_kosong
  4. Parse tanggal/jam/angka, validasi nilai domain             -> baris_tidak_valid
  5. Hapus duplikat Kode Transaksi di dalam file (ambil yg pertama) -> baris_duplikat
  6. Lewati Kode Transaksi yang sudah ada di database           -> baris_sudah_ada
Sisanya dimuat ke tabel transaksi (batch 5.000 baris) setelah data referensi (cabang, rute,
armada, member, jenis paket, kalender, jadwal) dipastikan ada.

Hanya memakai ORM / SQL standar supaya jalan di SQLite (lokal) maupun PostgreSQL (Railway).
"""
import io
import re
from datetime import date, datetime, timedelta

import pandas as pd
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.config import MIN_BARIS_ANALISIS
from app.load_data import buat_nama_acak
from app.models.models import (Armada, Cabang, JenisPaket, Member, Pengguna, RiwayatImport, Rute,
                               Transaksi)
from app.services.referensi_service import pastikan_jadwal, pastikan_kalender

# Header lengkap sesuai file sumber (data/kencana_transaksi_gabungan.csv).
KOLOM_CSV = ["Kode Transaksi", "Tanggal", "Hari", "Jam Keberangkatan", "Rute", "Cabang Asal",
             "Cabang Tujuan", "Layanan", "Armada", "Jenis Transaksi", "Jenis Paket", "Jumlah Unit",
             "Satuan", "Channel Pemesanan", "Jenis Member", "Harga Satuan", "Diskon per Tiket",
             "Total Harga", "Total Diskon", "Total Bayar", "Keterangan", "Nama Pelanggan"]

# Kolom yang harus ada di header DAN tidak boleh kosong di tiap baris. Kolom lain opsional:
# kalau tidak ada / kosong, nilainya diturunkan (mis. Cabang Asal dari Rute) atau diisi default.
KOLOM_WAJIB = ["Kode Transaksi", "Tanggal", "Jam Keberangkatan", "Rute", "Layanan",
               "Jenis Transaksi", "Jumlah Unit", "Channel Pemesanan", "Total Bayar"]

CONTOH_BARIS = [
    ["SMG240105ABCDE", "2024-01-05", "Friday", "05:00", "Semarang->Solo", "Semarang", "Solo", "VIP",
     "SMG-VIP-1", "Penumpang", "-", "2", "Orang", "Aplikasi", "Member Umum", "105000", "10000",
     "210000", "20000", "190000", "Reguler", ""],
    ["SLO240105FGHIJ", "2024-01-05", "Friday", "08:00", "Solo->Tayu", "Solo", "Tayu", "Reguler",
     "SOLO-REG-1", "Paket", "Elektronik", "7", "Kg", "Outlet", "-", "50000", "0", "50000", "0",
     "50000", "Reguler", ""]
]

MAKS_UKURAN_FILE = 50 * 1024 * 1024
BATCH = 5000
CHUNK_CEK_DB = 900          # di bawah batas 999 parameter SQLite lama
MAKS_SAMPEL_PER_JENIS = 5
TANGGAL_MIN = date(2000, 1, 1)
_RE_RUTE = re.compile(r"^([^-<>]+)->([^-<>]+)$")
_RE_JAM = re.compile(r"^(\d{1,2})[:.](\d{2})(?::\d{2})?$")

_LAYANAN = {"vip": "VIP", "reguler": "Reguler", "regular": "Reguler"}
_JENIS_TRX = {"penumpang": "Penumpang", "paket": "Paket"}
_BASIS_ARMADA = {"SOLO": "Solo", "SLO": "Solo", "SMG": "Semarang", "TAYU": "Tayu"}
_DISKON_MEMBER = {"Non-Member": 0, "Member Umum": 10000, "Member Mahasiswa": 15000}
_TARIF_PAKET = {"Reguler": (25000, 5000), "Elektronik": (40000, 5000)}


class ImportError400(ValueError):
    """Kesalahan pada file (bukan pada baris tertentu) -> dibalas HTTP 400."""


# --------------------------------------------------------------------------- baca file
def template_csv() -> str:
    buf = io.StringIO()
    pd.DataFrame(CONTOH_BARIS, columns=KOLOM_CSV).to_csv(buf, index=False)
    return buf.getvalue()


def _baca_csv(isi: bytes) -> pd.DataFrame:
    if not isi.strip():
        raise ImportError400("File kosong.")
    teks = None
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            teks = isi.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    baris_header = teks.splitlines()[0] if teks else ""
    # Excel versi Indonesia sering menyimpan CSV dengan pemisah ';'
    sep = ";" if baris_header.count(";") > baris_header.count(",") else ","
    try:
        df = pd.read_csv(io.StringIO(teks), sep=sep, dtype=str, keep_default_na=False,
                         skip_blank_lines=False, on_bad_lines="error")
    except Exception as e:  # noqa: BLE001
        raise ImportError400(f"File tidak bisa dibaca sebagai CSV: {e}") from e
    df.columns = [str(c).strip() for c in df.columns]
    return df


# --------------------------------------------------------------------------- cleaning
def _angka(s: pd.Series) -> pd.Series:
    bersih = s.str.replace(r"(?i)^rp\.?\s*", "", regex=True).str.replace(" ", "", regex=False)
    return pd.to_numeric(bersih, errors="coerce")


def _parse_tanggal(s: pd.Series) -> pd.Series:
    hasil = pd.to_datetime(s, format="%Y-%m-%d", errors="coerce")
    sisa = hasil.isna() & (s != "")
    if sisa.any():  # dukung juga format dd/mm/yyyy (hasil ekspor Excel)
        hasil[sisa] = pd.to_datetime(s[sisa], format="%d/%m/%Y", errors="coerce")
    return hasil


def _normal_jam(v: str):
    m = _RE_JAM.match(v)
    if not m:
        return None
    jam, menit = int(m.group(1)), int(m.group(2))
    if jam > 23 or menit > 59:
        return None
    return f"{jam:02d}:{menit:02d}"


def bersihkan(df: pd.DataFrame):
    """Kembalikan (df_bersih, hitungan, sampel_ditolak). df_bersih sudah bertipe benar."""
    hilang = [k for k in KOLOM_WAJIB if k not in df.columns]
    if hilang:
        raise ImportError400("Kolom wajib tidak ditemukan: " + ", ".join(hilang))

    for k in KOLOM_CSV:  # kolom opsional yang tidak ada -> kosong
        if k not in df.columns:
            df[k] = ""
    # Kolom pelanggan: "ID Pelanggan" (baru) atau "Nama Pelanggan" (lama); disimpan sebagai id_pelanggan.
    if "Nama Pelanggan" not in df.columns:
        df["Nama Pelanggan"] = df["ID Pelanggan"] if "ID Pelanggan" in df.columns else ""
    elif "ID Pelanggan" in df.columns:
        df["Nama Pelanggan"] = df["ID Pelanggan"].where(
            df["ID Pelanggan"].fillna("").astype(str).str.strip() != "", df["Nama Pelanggan"])
    df = df[KOLOM_CSV + ["Nama Pelanggan"]].copy()
    df["_baris"] = df.index + 2  # nomor baris di file (baris 1 = header)

    hitung = {"baris_sumber": 0, "baris_kosong_penuh": 0, "baris_kosong": 0,
              "baris_tidak_valid": 0, "baris_duplikat": 0, "baris_sudah_ada": 0}
    sampel: list[dict] = []

    def tolak(mask: pd.Series, kategori: str, alasan):
        """alasan: str atau Series sejajar df."""
        if not mask.any():
            return
        ditolak = df[mask]
        n_ambil = sum(1 for s in sampel if s["kategori"] == kategori)
        for idx, r in ditolak.head(max(0, MAKS_SAMPEL_PER_JENIS - n_ambil)).iterrows():
            sampel.append({
                "baris": int(r["_baris"]), "kode_transaksi": r["Kode Transaksi"] or None,
                "kategori": kategori,
                "alasan": alasan if isinstance(alasan, str) else alasan.loc[idx],
            })

    # 2. trim + baris kosong penuh
    kolom_teks = KOLOM_CSV + ["Nama Pelanggan"]
    for k in kolom_teks:
        df[k] = df[k].astype(str).str.strip()
    kosong_penuh = (df[KOLOM_CSV] == "").all(axis=1)
    hitung["baris_kosong_penuh"] = int(kosong_penuh.sum())
    df = df[~kosong_penuh]
    hitung["baris_sumber"] = len(df)

    # 3. kolom wajib kosong
    kosong = df[KOLOM_WAJIB] == ""
    m_kosong = kosong.any(axis=1)
    alasan_kosong = kosong.apply(lambda r: "Kolom wajib kosong: " + ", ".join(r.index[r]), axis=1) \
        if m_kosong.any() else ""
    tolak(m_kosong, "kosong", alasan_kosong)
    hitung["baris_kosong"] = int(m_kosong.sum())
    df = df[~m_kosong].copy()

    # 4. parse + validasi domain
    alasan = pd.Series("", index=df.index, dtype=object)

    def catat(mask, pesan):
        alasan[mask & (alasan == "")] = pesan

    tgl = _parse_tanggal(df["Tanggal"])
    batas_atas = pd.Timestamp(date.today() + timedelta(days=365))
    catat(tgl.isna(), "Format Tanggal tidak dikenali (pakai YYYY-MM-DD)")
    catat(tgl.notna() & ((tgl < pd.Timestamp(TANGGAL_MIN)) | (tgl > batas_atas)),
          "Tanggal di luar rentang wajar")

    jam = df["Jam Keberangkatan"].map(_normal_jam)
    catat(jam.isna(), "Jam Keberangkatan harus berformat HH:MM")

    rute_m = df["Rute"].str.replace(r"\s*->\s*", "->", regex=True).str.extract(_RE_RUTE)
    asal, tujuan = rute_m[0].str.strip(), rute_m[1].str.strip()
    catat(asal.isna() | tujuan.isna(), "Format Rute harus 'Asal->Tujuan'")
    catat(asal.notna() & (asal == tujuan), "Asal dan tujuan rute sama")
    cab_asal = df["Cabang Asal"].where(df["Cabang Asal"] != "", asal)
    cab_tujuan = df["Cabang Tujuan"].where(df["Cabang Tujuan"] != "", tujuan)
    catat(asal.notna() & (cab_asal != asal), "Cabang Asal tidak sesuai Rute")
    # Cabang Tujuan sengaja tidak dicek: data asli memuat titik turun yang berbeda dari kota
    # tujuan rute (mis. rute Tayu->Solo dengan Cabang Tujuan "Pati").

    layanan = df["Layanan"].str.lower().map(_LAYANAN)
    catat(layanan.isna(), "Layanan harus VIP atau Reguler")
    jenis = df["Jenis Transaksi"].str.lower().map(_JENIS_TRX)
    catat(jenis.isna(), "Jenis Transaksi harus Penumpang atau Paket")

    unit = _angka(df["Jumlah Unit"])
    catat(unit.isna(), "Jumlah Unit bukan angka")
    catat(unit.notna() & (unit <= 0), "Jumlah Unit harus > 0")
    bayar = _angka(df["Total Bayar"])
    catat(bayar.isna(), "Total Bayar bukan angka")
    catat(bayar.notna() & (bayar < 0), "Total Bayar tidak boleh negatif")

    angka_ops = {}
    for k in ["Harga Satuan", "Diskon per Tiket", "Total Harga", "Total Diskon"]:
        v = _angka(df[k])
        catat((df[k] != "") & v.isna(), f"{k} bukan angka")
        catat(v.notna() & (v < 0), f"{k} tidak boleh negatif")
        angka_ops[k] = v

    m_invalid = alasan != ""
    tolak(m_invalid, "tidak_valid", alasan)
    hitung["baris_tidak_valid"] = int(m_invalid.sum())
    ok = ~m_invalid

    total_diskon = angka_ops["Total Diskon"].fillna(0)
    total_harga = angka_ops["Total Harga"].fillna(bayar + total_diskon)
    harga_satuan = angka_ops["Harga Satuan"].fillna(total_harga / unit)
    satuan_default = jenis.map({"Penumpang": "Orang", "Paket": "Kg"})

    bersih = pd.DataFrame({
        "_baris": df["_baris"],
        "id_transaksi": df["Kode Transaksi"],
        "tanggal": tgl.dt.date,
        "hari": df["Hari"].where(df["Hari"] != "", tgl.dt.day_name()),
        "jam_keberangkatan": jam,
        "rute": asal + "->" + tujuan,
        "cabang_asal": asal,
        "cabang_tujuan": cab_tujuan,
        "layanan": layanan,
        "armada": df["Armada"].replace({"-": ""}),
        "jenis_transaksi": jenis,
        "jenis_paket": df["Jenis Paket"].replace({"-": ""}),
        "jumlah_unit": unit,
        "satuan": df["Satuan"].where(df["Satuan"] != "", satuan_default),
        "channel_pemesanan": df["Channel Pemesanan"],
        "jenis_member": df["Jenis Member"].replace({"-": ""}),
        "harga_satuan": harga_satuan,
        "diskon_per_tiket": angka_ops["Diskon per Tiket"].fillna(0),
        "total_harga": total_harga,
        "total_diskon": total_diskon,
        "total_bayar": bayar,
        "keterangan": df["Keterangan"],
        "id_pelanggan": df["Nama Pelanggan"],
    })[ok]
    df = df[ok]

    # 5. duplikat di dalam file
    m_dup = bersih["id_transaksi"].duplicated(keep="first")
    tolak(m_dup.reindex(df.index, fill_value=False), "duplikat",
          "Kode Transaksi muncul lebih dari sekali di file")
    hitung["baris_duplikat"] = int(m_dup.sum())
    bersih = bersih[~m_dup]
    df = df[~m_dup.reindex(df.index, fill_value=False)]

    return bersih, df, hitung, sampel, tolak


def _kode_sudah_ada(db: Session, kode: list[str]) -> set[str]:
    ada = set()
    for i in range(0, len(kode), CHUNK_CEK_DB):
        potong = kode[i:i + CHUNK_CEK_DB]
        ada.update(db.scalars(select(Transaksi.id_transaksi)
                              .where(Transaksi.id_transaksi.in_(potong))))
    return ada


# --------------------------------------------------------------------------- referensi
def _siapkan_referensi(db: Session, df: pd.DataFrame, simpan: bool) -> dict:
    """Buat cabang/rute/armada/member/jenis paket yang belum ada. Kembalikan map nama->id
    dan jumlah entitas baru. Saat simpan=False (dry run) hanya dihitung, tidak ditulis."""
    baru = {"cabang": 0, "rute": 0, "armada": 0, "member": 0, "jenis_paket": 0}

    cabang_ada = set(db.scalars(select(Cabang.nama_cabang)))
    # Cabang diturunkan dari ujung-ujung rute (FK rute.cabang_asal/cabang_tujuan), bukan dari
    # kolom Cabang Tujuan yang bisa berisi titik turun (mis. "Pati").
    ujung_rute = {k for nama_rute in df["rute"].unique() for k in nama_rute.split("->")}
    for nama in sorted(ujung_rute):
        if nama not in cabang_ada:
            baru["cabang"] += 1
            if simpan:
                db.add(Cabang(nama_cabang=nama, kota=nama))
                cabang_ada.add(nama)
    if simpan:
        db.flush()

    rute_map = {r.nama_rute: r for r in db.scalars(select(Rute))}
    for nama_rute, grp in df.groupby("rute"):
        layanan_baru = set(grp["layanan"].unique())
        r = rute_map.get(nama_rute)
        if r is None:
            baru["rute"] += 1
            if simpan:
                asal, tujuan = nama_rute.split("->")
                r = Rute(nama_rute=nama_rute, cabang_asal=asal, cabang_tujuan=tujuan,
                         layanan_tersedia=",".join(sorted(layanan_baru)))
                db.add(r)
                rute_map[nama_rute] = r
        elif simpan:
            gabung = set(filter(None, (r.layanan_tersedia or "").split(","))) | layanan_baru
            r.layanan_tersedia = ",".join(sorted(gabung))
    if simpan:
        db.flush()

    armada_map = dict(db.execute(select(Armada.kode_armada, Armada.id_armada)).all())
    for kode, grp in df[df["armada"] != ""].groupby("armada"):
        if kode in armada_map:
            continue
        baru["armada"] += 1
        if simpan:
            layanan = grp["layanan"].iloc[0]
            basis = _BASIS_ARMADA.get(kode.split("-")[0].upper())
            a = Armada(kode_armada=kode, tipe_layanan=layanan,
                       jenis_kendaraan="Toyota HiAce Premio" if layanan == "VIP"
                       else "Toyota HiAce Commuter",
                       kapasitas=8 if layanan == "VIP" else 12,
                       basis_outlet=basis if basis in cabang_ada else None)
            db.add(a)
            db.flush()
            armada_map[kode] = a.id_armada

    member_map = dict(db.execute(select(Member.jenis_member, Member.id_member)).all())
    for jm in sorted(set(df["jenis_member"]) - {""}):
        if jm not in member_map:
            baru["member"] += 1
            if simpan:
                m = Member(jenis_member=jm, diskon_per_tiket=_DISKON_MEMBER.get(jm, 0))
                db.add(m)
                db.flush()
                member_map[jm] = m.id_member

    paket_map = dict(db.execute(select(JenisPaket.nama_paket, JenisPaket.id_jenis_paket)).all())
    for jp in sorted(set(df.loc[df["jenis_transaksi"] == "Paket", "jenis_paket"]) - {""}):
        if jp not in paket_map:
            baru["jenis_paket"] += 1
            if simpan:
                dasar, lanjut = _TARIF_PAKET.get(jp, (25000, 5000))
                p = JenisPaket(nama_paket=jp, tarif_5kg_pertama=dasar, tarif_per_kg_lanjut=lanjut)
                db.add(p)
                db.flush()
                paket_map[jp] = p.id_jenis_paket

    return {"baru": baru, "rute": {k: v.id_rute for k, v in rute_map.items()},
            "armada": armada_map, "member": member_map, "paket": paket_map}


def _checkpoint(db: Session):
    """PostgreSQL: CHECKPOINT agar WAL dari bulk insert tidak memenuhi volume kecil."""
    if db.bind.dialect.name != "postgresql":
        return
    try:
        db.execute(text("CHECKPOINT"))
        db.commit()
    except Exception:  # noqa: BLE001 - user tanpa hak CHECKPOINT: abaikan
        db.rollback()


# --------------------------------------------------------------------------- status data
def status_data(db: Session) -> dict:
    total, mulai, selesai = db.execute(
        select(func.count(Transaksi.id_transaksi), func.min(Transaksi.tanggal),
               func.max(Transaksi.tanggal))).one()
    terakhir = db.scalars(select(RiwayatImport).order_by(RiwayatImport.waktu.desc()).limit(1)).first()
    return {
        "total_baris": int(total or 0),
        "tanggal_awal": mulai,
        "tanggal_akhir": selesai,
        "min_baris_analisis": MIN_BARIS_ANALISIS,
        "siap_dianalisis": int(total or 0) >= MIN_BARIS_ANALISIS,
        "import_terakhir": riwayat_ke_dict(db, terakhir) if terakhir else None,
    }


def riwayat_ke_dict(db: Session, r: RiwayatImport, nama_pengguna: str | None = None) -> dict:
    if nama_pengguna is None and r.diimport_oleh:
        p = db.get(Pengguna, r.diimport_oleh)
        nama_pengguna = p.nama if p else None
    return {
        "id_import": r.id_import, "nama_file": r.nama_file, "waktu": r.waktu,
        "diimport_oleh": nama_pengguna, "baris_sumber": r.baris_sumber,
        "baris_kosong": r.baris_kosong, "baris_tidak_valid": r.baris_tidak_valid,
        "baris_duplikat": r.baris_duplikat, "baris_sudah_ada": r.baris_sudah_ada,
        "baris_dimuat": r.baris_dimuat, "status": r.status, "catatan": r.catatan,
    }


def daftar_riwayat(db: Session, limit: int = 50) -> list[dict]:
    rows = db.execute(select(RiwayatImport, Pengguna.nama)
                      .outerjoin(Pengguna, Pengguna.id_pengguna == RiwayatImport.diimport_oleh)
                      .order_by(RiwayatImport.waktu.desc()).limit(limit)).all()
    return [riwayat_ke_dict(db, r, nama or "-") for r, nama in rows]


# --------------------------------------------------------------------------- proses utama
def import_transaksi(db: Session, isi: bytes, nama_file: str, pengguna: Pengguna | None,
                     dry_run: bool = False) -> dict:
    df_mentah = _baca_csv(isi)
    bersih, df_ref, hitung, sampel, tolak = bersihkan(df_mentah)

    # 6. sudah ada di database
    ada = _kode_sudah_ada(db, bersih["id_transaksi"].tolist())
    m_ada = bersih["id_transaksi"].isin(ada)
    tolak(m_ada.reindex(df_ref.index, fill_value=False), "sudah_ada",
          "Kode Transaksi sudah ada di database")
    hitung["baris_sudah_ada"] = int(m_ada.sum())
    bersih = bersih[~m_ada]

    ref = _siapkan_referensi(db, bersih, simpan=not dry_run)
    dimuat = 0
    status, catatan = "berhasil", None

    if not dry_run and len(bersih):
        try:
            tgl_min, tgl_max = min(bersih["tanggal"]), max(bersih["tanggal"])
            pastikan_kalender(db, tgl_min, tgl_max + timedelta(days=400))
            kombinasi = bersih[["rute", "jam_keberangkatan", "layanan"]].drop_duplicates()
            pastikan_jadwal(db, ((ref["rute"][r], j, l)
                                 for r, j, l in kombinasi.itertuples(index=False)))
            db.commit()

            batch = []
            for d in bersih.to_dict(orient="records"):
                id_plg = d["id_pelanggan"] or (
                    buat_nama_acak(d["id_transaksi"]) if d["jenis_transaksi"] == "Penumpang" else None)
                batch.append({
                    "id_transaksi": d["id_transaksi"], "tanggal": d["tanggal"], "hari": d["hari"],
                    "jam_keberangkatan": d["jam_keberangkatan"], "id_rute": ref["rute"][d["rute"]],
                    "id_armada": ref["armada"].get(d["armada"]),
                    "cabang_asal": d["cabang_asal"], "cabang_tujuan": d["cabang_tujuan"],
                    "layanan": d["layanan"], "jenis_transaksi": d["jenis_transaksi"],
                    "id_member": ref["member"].get(d["jenis_member"]),
                    "id_jenis_paket": ref["paket"].get(d["jenis_paket"])
                    if d["jenis_transaksi"] == "Paket" else None,
                    "jumlah_unit": float(d["jumlah_unit"]), "satuan": d["satuan"],
                    "channel_pemesanan": d["channel_pemesanan"],
                    "harga_satuan": float(d["harga_satuan"]),
                    "diskon_per_tiket": float(d["diskon_per_tiket"]),
                    "total_harga": float(d["total_harga"]), "total_diskon": float(d["total_diskon"]),
                    "total_bayar": float(d["total_bayar"]), "keterangan": d["keterangan"] or None,
                    "id_pelanggan": id_plg,
                })
                if len(batch) >= BATCH:
                    db.bulk_insert_mappings(Transaksi, batch)
                    db.commit()
                    dimuat += len(batch)
                    batch = []
                    if dimuat % 50_000 == 0:
                        _checkpoint(db)
            if batch:
                db.bulk_insert_mappings(Transaksi, batch)
                db.commit()
                dimuat += len(batch)
            _checkpoint(db)
        except Exception as e:  # noqa: BLE001
            db.rollback()
            status = "gagal"
            catatan = (f"Import terhenti setelah {dimuat:,} baris: "
                       f"{e.__class__.__name__}: {str(e)[:300]}")

    if not dry_run:
        db.add(RiwayatImport(
            nama_file=nama_file[:255], diimport_oleh=pengguna.id_pengguna if pengguna else None,
            baris_sumber=hitung["baris_sumber"], baris_duplikat=hitung["baris_duplikat"],
            baris_sudah_ada=hitung["baris_sudah_ada"], baris_kosong=hitung["baris_kosong"],
            baris_tidak_valid=hitung["baris_tidak_valid"], baris_dimuat=dimuat, status=status,
            catatan=catatan or (f"{hitung['baris_kosong_penuh']} baris kosong total dibuang"
                                if hitung["baris_kosong_penuh"] else None),
        ))
        db.commit()

    total_db = int(db.scalar(select(func.count(Transaksi.id_transaksi))) or 0)
    total_setelah = total_db if not dry_run else total_db + len(bersih)
    sampel.sort(key=lambda s: s["baris"])
    return {
        "dry_run": dry_run,
        "nama_file": nama_file,
        "status": status,
        "catatan": catatan,
        **hitung,
        "baris_lolos": int(len(bersih)),
        "baris_dimuat": dimuat,
        "entitas_baru": ref["baru"],
        "sampel_ditolak": sampel[:20],
        "total_data_setelah": total_setelah,
        "min_baris_analisis": MIN_BARIS_ANALISIS,
        "siap_dianalisis": total_setelah >= MIN_BARIS_ANALISIS,
    }
