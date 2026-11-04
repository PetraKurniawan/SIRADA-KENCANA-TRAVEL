"""
Service Exploratory Data Analysis (EDA) - tahap "Exploratory Data Analysis" pada flowchart.

Semua agregasi berat dilakukan di database (GROUP BY) agar tetap cepat pada ratusan ribu baris,
dan hanya hasil ringkas (per tanggal / per kombinasi kategori / per nilai unik) yang ditarik
ke pandas. Query sengaja memakai fungsi SQL standar (COUNT/SUM/MIN/MAX/CASE) agar jalan di
SQLite maupun PostgreSQL. Total hanya 4 query besar atas tabel transaksi, dijalankan paralel.
"""
import copy
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import numpy as np
import pandas as pd
from scipy.stats import kurtosis, skew
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.core.config import MIN_BARIS_ANALISIS
from app.models.models import Kalender, Member, Rute, Transaksi

NAMA_HARI = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
NAMA_BULAN = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]

# Kolom penting yang dicek kelengkapannya (jumlah nilai NULL).
KOLOM_KELENGKAPAN = [
    "tanggal", "jam_keberangkatan", "id_rute", "cabang_asal", "cabang_tujuan", "layanan",
    "jenis_transaksi", "id_member", "id_armada", "jumlah_unit", "channel_pemesanan",
    "harga_satuan", "total_bayar", "id_pelanggan",
]
KATEGORI_DISTRIBUSI = ("rute", "layanan", "channel_pemesanan", "jenis_member", "jenis_transaksi")

_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAKS = 64

_PENUMPANG = Transaksi.jenis_transaksi == "Penumpang"


def _filter(q, cabang: str | None, mulai: date | None, selesai: date | None):
    if cabang:
        q = q.filter(Transaksi.cabang_asal == cabang)
    if mulai:
        q = q.filter(Transaksi.tanggal >= mulai)
    if selesai:
        q = q.filter(Transaksi.tanggal <= selesai)
    return q


def _sum_penumpang():
    """Jumlah penumpang = SUM(jumlah_unit) khusus transaksi Penumpang (Paket diisi 0)."""
    return func.sum(case((_PENUMPANG, Transaksi.jumlah_unit), else_=0))


def _ke_tanggal(v) -> date | None:
    if v is None:
        return None
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _ringkasan_dan_kelengkapan(db, f) -> tuple[dict, list[dict]]:
    """Satu query: ukuran dataset, rentang tanggal, jumlah rute/pelanggan, dan COUNT(kolom) per
    kolom penting (COUNT mengabaikan NULL, sehingga jumlah NULL = total - COUNT(kolom))."""
    kolom = [getattr(Transaksi, k) for k in KOLOM_KELENGKAPAN]
    r = f(db.query(
        func.count(Transaksi.id_transaksi),
        func.min(Transaksi.tanggal),
        func.max(Transaksi.tanggal),
        func.count(func.distinct(Transaksi.id_rute)),
        func.count(func.distinct(Transaksi.id_pelanggan)),
        _sum_penumpang(),
        func.sum(Transaksi.total_bayar),
        func.count(func.distinct(Transaksi.cabang_asal)),
        *[func.count(c) for c in kolom],
    )).one()
    n = int(r[0] or 0)
    mulai, selesai = _ke_tanggal(r[1]), _ke_tanggal(r[2])
    ringkasan = {
        "jumlah_baris": n,
        "tanggal_awal": mulai.isoformat() if mulai else None,
        "tanggal_akhir": selesai.isoformat() if selesai else None,
        "rentang_hari": ((selesai - mulai).days + 1) if mulai and selesai else 0,
        "jumlah_rute": int(r[3] or 0),
        "jumlah_pelanggan": int(r[4] or 0),
        "jumlah_cabang": int(r[7] or 0),
        "total_penumpang": int(r[5] or 0),
        "total_pendapatan": float(r[6] or 0),
        "min_baris_analisis": MIN_BARIS_ANALISIS,
        "memenuhi_min_baris": n >= MIN_BARIS_ANALISIS,
    }
    kelengkapan = []
    for nama, terisi in zip(KOLOM_KELENGKAPAN, r[8:]):
        terisi = int(terisi or 0)
        kosong = n - terisi
        kelengkapan.append({
            "kolom": nama,
            "terisi": terisi,
            "kosong": kosong,
            "persen_kosong": round(100 * kosong / n, 2) if n else 0.0,
        })
    return ringkasan, kelengkapan


def _describe(nilai: np.ndarray, bobot: np.ndarray, nama: str, keterangan: str) -> dict:
    """Statistik deskriptif dari distribusi nilai unik + frekuensinya. Hasilnya identik dengan
    pandas.describe() atas seluruh baris, tetapi tanpa menarik seluruh baris dari database."""
    hasil = {"variabel": nama, "keterangan": keterangan, "count": 0, "mean": None, "std": None,
             "min": None, "q1": None, "median": None, "q3": None, "max": None,
             "skewness": None, "kurtosis": None, "batas_bawah": None, "batas_atas": None,
             "jumlah_outlier": 0, "persen_outlier": 0.0, "bentuk": None}
    mask = ~np.isnan(nilai)
    nilai, bobot = nilai[mask], bobot[mask]
    if bobot.sum() == 0:
        return hasil
    data = np.repeat(nilai, bobot)
    q1, med, q3 = np.percentile(data, [25, 50, 75])
    hasil.update({
        "count": int(data.size),
        "mean": round(float(data.mean()), 2),
        "std": round(float(data.std(ddof=1)), 2) if data.size > 1 else 0.0,
        "min": float(data.min()), "q1": float(q1), "median": float(med),
        "q3": float(q3), "max": float(data.max()),
    })
    # Bentuk distribusi & pencilan (aturan Tukey: di luar Q1 - 1,5 IQR .. Q3 + 1,5 IQR).
    iqr = q3 - q1
    bawah, atas = float(q1 - 1.5 * iqr), float(q3 + 1.5 * iqr)
    n_out = int(((data < bawah) | (data > atas)).sum())
    if data.size > 2 and data.std() > 0:
        sk, ku = float(skew(data, bias=False)), float(kurtosis(data, bias=False))
        hasil.update({"skewness": round(sk, 3), "kurtosis": round(ku, 3),
                      "bentuk": ("miring kanan" if sk > 0.5 else "miring kiri" if sk < -0.5 else "relatif simetris")})
    hasil.update({"batas_bawah": round(bawah, 2), "batas_atas": round(atas, 2),
                  "jumlah_outlier": n_out, "persen_outlier": round(100 * n_out / data.size, 2)})
    return hasil


def _statistik(db, f) -> list[dict]:
    """Satu GROUP BY atas kombinasi nilai (jumlah_unit, total_bayar, harga_satuan) transaksi
    Penumpang; jumlah kombinasi unik kecil sehingga murah ditarik ke pandas."""
    grup = (Transaksi.jumlah_unit, Transaksi.total_bayar, Transaksi.harga_satuan)
    rows = f(db.query(*grup, func.count(Transaksi.id_transaksi)).filter(_PENUMPANG)) \
        .group_by(*grup).all()
    df = pd.DataFrame(rows, columns=["jumlah_unit", "total_bayar", "harga_satuan", "n"])
    spek = [("jumlah_unit", "Jumlah penumpang per transaksi"),
            ("total_bayar", "Total bayar per transaksi penumpang (Rp)"),
            ("harga_satuan", "Harga tiket per penumpang (Rp)")]
    hasil = []
    for kolom, ket in spek:
        if df.empty:
            hasil.append(_describe(np.array([]), np.array([], dtype=int), kolom, ket))
            continue
        g = df.groupby(kolom)["n"].sum()
        hasil.append(_describe(np.asarray(g.index, dtype=float), np.asarray(g.values, dtype=int), kolom, ket))
    return hasil


def _harian(db, f) -> pd.DataFrame:
    rows = f(db.query(
        Transaksi.tanggal,
        func.count(Transaksi.id_transaksi),
        _sum_penumpang(),
        func.sum(Transaksi.total_bayar),
    )).group_by(Transaksi.tanggal).all()
    df = pd.DataFrame(rows, columns=["tanggal", "transaksi", "penumpang", "pendapatan"])
    if df.empty:
        return df
    df["tanggal"] = pd.to_datetime(df["tanggal"].map(_ke_tanggal))
    df[["transaksi", "penumpang", "pendapatan"]] = df[["transaksi", "penumpang", "pendapatan"]].astype(float)
    return df


def _lengkapi_hari(db, harian: pd.DataFrame) -> pd.DataFrame:
    """Gabungkan agregat harian dengan tabel kalender untuk tiap hari dalam rentang data
    (hari tanpa transaksi diisi 0 agar rata-rata harian tidak bias)."""
    awal, akhir = harian["tanggal"].min().date(), harian["tanggal"].max().date()
    kal = db.query(Kalender.tanggal, Kalender.weekend, Kalender.libur_nasional, Kalender.libur_sekolah) \
            .filter(Kalender.tanggal >= awal, Kalender.tanggal <= akhir).all()
    kal = pd.DataFrame(kal, columns=["tanggal", "weekend", "libur_nasional", "libur_sekolah"])
    semua = pd.DataFrame({"tanggal": pd.date_range(awal, akhir, freq="D")})
    if not kal.empty:
        kal["tanggal"] = pd.to_datetime(kal["tanggal"].map(_ke_tanggal))
        semua = semua.merge(kal, on="tanggal", how="left")
    else:
        for k in ("weekend", "libur_nasional", "libur_sekolah"):
            semua[k] = None
    semua["ada_kalender"] = semua["libur_nasional"].notna()
    # Hari tanpa entri kalender: weekend diturunkan dari tanggal, libur dianggap tidak.
    semua["weekend"] = semua["weekend"].where(semua["weekend"].notna(), semua["tanggal"].dt.dayofweek >= 5)
    for k in ("weekend", "libur_nasional", "libur_sekolah"):
        semua[k] = semua[k].fillna(False).astype(bool)
    semua = semua.merge(harian, on="tanggal", how="left")
    semua[["transaksi", "penumpang", "pendapatan"]] = semua[["transaksi", "penumpang", "pendapatan"]].fillna(0.0)
    return semua


def _tren_bulanan(harian: pd.DataFrame) -> list[dict]:
    b = harian.groupby(harian["tanggal"].dt.to_period("M")).agg(
        transaksi=("transaksi", "sum"), penumpang=("penumpang", "sum"),
        pendapatan=("pendapatan", "sum"), hari_aktif=("transaksi", "size"))
    return [{
        "bulan": str(p),
        "label": f"{NAMA_BULAN[p.month - 1]} {str(p.year)[2:]}",
        "transaksi": int(r.transaksi),
        "penumpang": int(r.penumpang),
        "pendapatan": float(r.pendapatan),
        "hari_aktif": int(r.hari_aktif),
    } for p, r in b.iterrows()]


def _pola_hari(hari: pd.DataFrame) -> list[dict]:
    g = hari.groupby(hari["tanggal"].dt.dayofweek).agg(
        jumlah_hari=("tanggal", "size"), rata_penumpang=("penumpang", "mean"),
        rata_pendapatan=("pendapatan", "mean"), rata_transaksi=("transaksi", "mean"))
    return [{
        "hari": NAMA_HARI[int(i)],
        "urutan": int(i),
        "jumlah_hari": int(r.jumlah_hari),
        "rata_penumpang": round(float(r.rata_penumpang), 1),
        "rata_transaksi": round(float(r.rata_transaksi), 1),
        "rata_pendapatan": round(float(r.rata_pendapatan), 0),
    } for i, r in g.iterrows()]


def _efek(hari: pd.DataFrame, kolom: str, nama: str, label_ya: str, label_tidak: str) -> dict:
    """Bandingkan rata-rata penumpang harian pada hari berstatus `kolom` vs hari lainnya."""
    ya, tidak = hari[hari[kolom]], hari[~hari[kolom]]
    rata_ya = float(ya["penumpang"].mean()) if len(ya) else None
    rata_tidak = float(tidak["penumpang"].mean()) if len(tidak) else None
    selisih = (round(100 * (rata_ya - rata_tidak) / rata_tidak, 1)
               if rata_ya is not None and rata_tidak else None)
    return {
        "faktor": kolom,
        "nama": nama,
        "label_ya": label_ya,
        "label_tidak": label_tidak,
        "jumlah_hari_ya": int(len(ya)),
        "jumlah_hari_tidak": int(len(tidak)),
        "rata_penumpang_ya": None if rata_ya is None else round(rata_ya, 1),
        "rata_penumpang_tidak": None if rata_tidak is None else round(rata_tidak, 1),
        "rata_pendapatan_ya": round(float(ya["pendapatan"].mean()), 0) if len(ya) else None,
        "rata_pendapatan_tidak": round(float(tidak["pendapatan"].mean()), 0) if len(tidak) else None,
        "selisih_persen": selisih,
    }


def _ke_distribusi(df: pd.DataFrame, kolom: str) -> list[dict]:
    g = df.groupby(kolom)[["transaksi", "penumpang", "pendapatan"]].sum()
    total = float(g["transaksi"].sum()) or 1.0
    total_pend = float(g["pendapatan"].sum()) or 1.0
    hasil = [{
        "kategori": str(k),
        "transaksi": int(r.transaksi),
        "penumpang": int(r.penumpang),
        "pendapatan": float(r.pendapatan),
        "persen_transaksi": round(100 * float(r.transaksi) / total, 2),
        "persen_pendapatan": round(100 * float(r.pendapatan) / total_pend, 2),
    } for k, r in g.iterrows()]
    return sorted(hasil, key=lambda x: -x["transaksi"])


def _distribusi(db, f) -> dict:
    """Satu GROUP BY gabungan (rute x layanan x channel x member x jenis transaksi; hanya ratusan
    kombinasi), lalu tiap distribusi dijumlahkan ulang di pandas. Nama rute & jenis member
    diambil dari tabel master kecil, bukan lewat JOIN pada tabel transaksi."""
    kolom = (Transaksi.id_rute, Transaksi.layanan, Transaksi.channel_pemesanan,
             Transaksi.id_member, Transaksi.jenis_transaksi)
    rows = f(db.query(*kolom, func.count(Transaksi.id_transaksi), _sum_penumpang(),
                      func.sum(Transaksi.total_bayar))).group_by(*kolom).all()
    df = pd.DataFrame(rows, columns=["id_rute", "layanan", "channel_pemesanan", "id_member",
                                     "jenis_transaksi", "transaksi", "penumpang", "pendapatan"])
    if df.empty:
        return {k: [] for k in KATEGORI_DISTRIBUSI}
    nama_rute = dict(db.query(Rute.id_rute, Rute.nama_rute).all())
    nama_member = dict(db.query(Member.id_member, Member.jenis_member).all())
    df["rute"] = df["id_rute"].map(lambda v: nama_rute.get(v, v) if v else "(kosong)")
    df["jenis_member"] = df["id_member"].map(
        lambda v: nama_member.get(v, "Tanpa Data Member") if v else "Tanpa Data Member")
    for k in ("layanan", "channel_pemesanan", "jenis_transaksi"):
        df[k] = df[k].fillna("(kosong)").replace("", "(kosong)")
    df[["transaksi", "penumpang", "pendapatan"]] = df[["transaksi", "penumpang", "pendapatan"]].astype(float)
    return {k: _ke_distribusi(df, k) for k in KATEGORI_DISTRIBUSI}


def _pola_jam(db, f) -> list[dict]:
    """Permintaan per hari x jam keberangkatan (heatmap pola permintaan)."""
    rows = f(db.query(Transaksi.hari, Transaksi.jam_keberangkatan, _sum_penumpang(),
                      func.count(func.distinct(Transaksi.tanggal)))) \
        .group_by(Transaksi.hari, Transaksi.jam_keberangkatan).all()
    hasil = []
    for hari, jam, penumpang, n_hari in rows:
        if hari not in NAMA_HARI or not jam:
            continue
        hasil.append({"hari": hari, "jam": str(jam)[:5], "penumpang": int(penumpang or 0),
                      "rata_per_hari": round(float(penumpang or 0) / max(int(n_hari or 1), 1), 2)})
    return sorted(hasil, key=lambda x: (NAMA_HARI.index(x["hari"]), x["jam"]))


def _korelasi(hari: pd.DataFrame) -> dict:
    """Matriks korelasi Pearson antar-variabel harian (penumpang, pendapatan, dan dummy kalender).
    Korelasi dummy (0/1) dengan penumpang = point-biserial. Dasar pemilihan variabel eksogen
    SARIMAX pada modul Forecasting."""
    kolom = {"penumpang": "Penumpang", "transaksi": "Transaksi", "pendapatan": "Pendapatan",
             "weekend": "Akhir pekan", "libur_nasional": "Libur nasional", "libur_sekolah": "Libur sekolah"}
    df = hari[list(kolom)].astype(float)
    df = df.loc[:, df.std() > 0]
    if df.shape[1] < 2:
        return {"variabel": [], "label": [], "matriks": []}
    m = df.corr(method="pearson").fillna(0.0)
    return {"variabel": list(m.columns), "label": [kolom[c] for c in m.columns],
            "matriks": [[round(float(v), 3) for v in row] for row in m.to_numpy()]}


def _outlier_harian(hari: pd.DataFrame) -> dict:
    """Pencilan pada total penumpang harian (aturan Tukey / IQR)."""
    y = hari["penumpang"].astype(float)
    if len(y) < 8:
        return {"jumlah_hari": int(len(y)), "jumlah_outlier": 0, "persen_outlier": 0.0, "daftar": []}
    q1, q3 = y.quantile(0.25), y.quantile(0.75)
    iqr = q3 - q1
    bawah, atas = float(q1 - 1.5 * iqr), float(q3 + 1.5 * iqr)
    mask = (y < bawah) | (y > atas)
    df = hari.loc[mask, ["tanggal", "penumpang", "weekend", "libur_nasional", "libur_sekolah"]].copy()
    df["jarak"] = np.where(df["penumpang"] > atas, df["penumpang"] - atas, bawah - df["penumpang"])
    top = df.sort_values("jarak", ascending=False).head(8)
    daftar = []
    for _, r in top.iterrows():
        sebab = [n for k, n in (("libur_nasional", "libur nasional"), ("libur_sekolah", "libur sekolah"),
                                ("weekend", "akhir pekan")) if bool(r[k])]
        daftar.append({"tanggal": r["tanggal"].date().isoformat(), "penumpang": int(r["penumpang"]),
                       "arah": "tinggi" if r["penumpang"] > atas else "rendah",
                       "kalender": ", ".join(sebab) if sebab else "hari biasa"})
    return {"jumlah_hari": int(len(y)), "batas_bawah": round(bawah, 1), "batas_atas": round(atas, 1),
            "jumlah_outlier": int(mask.sum()), "persen_outlier": round(100 * float(mask.mean()), 2),
            "daftar": daftar}


def _deret_waktu(hari: pd.DataFrame) -> dict:
    """Karakter deret waktu penumpang harian: uji stasioneritas ADF dan autokorelasi (ACF).
    Lonjakan ACF di lag 7 = pola mingguan -> alasan memakai SARIMA/SARIMAX/Holt-Winters musiman."""
    from statsmodels.tsa.stattools import acf, adfuller
    y = hari.sort_values("tanggal")["penumpang"].astype(float).to_numpy()
    if len(y) < 30 or np.std(y) == 0:
        return {"adf_p_value": None, "stasioner": None, "acf": [], "musiman_mingguan": None}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            p = float(adfuller(y, autolag="AIC")[1])
    except Exception:  # noqa: BLE001
        p = None
    lag_maks = min(28, len(y) // 3)
    nilai = acf(y, nlags=lag_maks, fft=True)
    batas = 1.96 / np.sqrt(len(y))
    acf_list = [{"lag": i, "acf": round(float(nilai[i]), 3)} for i in range(1, lag_maks + 1)]
    lag7 = next((a["acf"] for a in acf_list if a["lag"] == 7), None)
    return {"adf_p_value": None if p is None else round(p, 4),
            "stasioner": None if p is None else bool(p < 0.05),
            "acf": acf_list, "batas_signifikan": round(float(batas), 3),
            "acf_lag7": lag7, "musiman_mingguan": None if lag7 is None else bool(lag7 > batas)}


def _signature(db, cabang: str | None):
    q = db.query(func.count(Transaksi.id_transaksi), func.max(Transaksi.tanggal))
    if cabang:
        q = q.filter(Transaksi.cabang_asal == cabang)
    n, maks = q.one()
    return int(n or 0), str(maks) if maks else None


def _paralel(db: Session, tugas: dict) -> dict:
    """Jalankan beberapa query agregasi independen secara paralel, masing-masing dengan
    session/koneksi sendiri (driver DB melepas GIL saat query berjalan). Untuk SQLite in-memory
    (dipakai di test) dijalankan berurutan pada session yang sama."""
    engine = db.get_bind()
    url = engine.url
    if url.get_backend_name() == "sqlite" and (not url.database or url.database == ":memory:"):
        return {k: fn(db) for k, fn in tugas.items()}

    def jalankan(fn):
        with Session(bind=engine) as sesi:
            return fn(sesi)

    with ThreadPoolExecutor(max_workers=len(tugas)) as ex:
        futures = {k: ex.submit(jalankan, fn) for k, fn in tugas.items()}
        return {k: fut.result() for k, fut in futures.items()}


def _hitung(db: Session, cabang: str | None, mulai: date | None, selesai: date | None) -> dict:
    f = lambda q: _filter(q, cabang, mulai, selesai)  # noqa: E731
    hasil = _paralel(db, {
        "ringkasan": lambda s: _ringkasan_dan_kelengkapan(s, f),
        "statistik": lambda s: _statistik(s, f),
        "harian": lambda s: _harian(s, f),
        "distribusi": lambda s: _distribusi(s, f),
        "pola_jam": lambda s: _pola_jam(s, f),
    })
    ringkasan, kelengkapan = hasil["ringkasan"]
    harian = hasil["harian"]
    filter_info = {"cabang": cabang,
                   "tanggal_mulai": mulai.isoformat() if mulai else None,
                   "tanggal_selesai": selesai.isoformat() if selesai else None}
    if ringkasan["jumlah_baris"] == 0 or harian.empty:
        return {"filter": filter_info, "ringkasan": ringkasan, "kelengkapan": kelengkapan,
                "statistik": [], "tren_bulanan": [], "pola_hari": [], "efek_kalender": [],
                "pola_jam": [], "korelasi": {"variabel": [], "label": [], "matriks": []},
                "outlier_harian": {"jumlah_hari": 0, "jumlah_outlier": 0, "persen_outlier": 0.0, "daftar": []},
                "deret_waktu": {"adf_p_value": None, "stasioner": None, "acf": [], "musiman_mingguan": None},
                "hari_tanpa_kalender": 0, "distribusi": {k: [] for k in KATEGORI_DISTRIBUSI}}

    hari = _lengkapi_hari(db, harian)
    efek = [
        _efek(hari, "weekend", "Akhir Pekan", "Akhir pekan", "Hari kerja"),
        _efek(hari, "libur_nasional", "Libur Nasional", "Libur nasional", "Hari biasa"),
        _efek(hari, "libur_sekolah", "Libur Sekolah", "Libur sekolah", "Hari biasa"),
    ]
    return {
        "filter": filter_info,
        "ringkasan": ringkasan,
        "kelengkapan": kelengkapan,
        "statistik": hasil["statistik"],
        "tren_bulanan": _tren_bulanan(harian),
        "pola_hari": _pola_hari(hari),
        "efek_kalender": efek,
        "pola_jam": hasil["pola_jam"],
        "korelasi": _korelasi(hari),
        "outlier_harian": _outlier_harian(hari),
        "deret_waktu": _deret_waktu(hari),
        "hari_tanpa_kalender": int((~hari["ada_kalender"]).sum()),
        "distribusi": hasil["distribusi"],
    }


def analisis_eda(db: Session, cabang: str | None = None, tanggal_mulai: date | None = None,
                 tanggal_selesai: date | None = None) -> dict:
    """Ringkasan EDA atas tabel transaksi (+ kalender, rute, member). Di-cache per
    (cabang, rentang tanggal, signature data = jumlah baris + tanggal terakhir)."""
    kunci = (cabang, tanggal_mulai, tanggal_selesai, _signature(db, cabang))
    with _CACHE_LOCK:
        hasil = _CACHE.get(kunci)
    if hasil is None:
        hasil = _hitung(db, cabang, tanggal_mulai, tanggal_selesai)
        with _CACHE_LOCK:
            if len(_CACHE) >= _CACHE_MAKS:
                _CACHE.pop(next(iter(_CACHE)))
            _CACHE[kunci] = hasil
    return copy.deepcopy(hasil)
