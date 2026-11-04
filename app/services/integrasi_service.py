"""
Service Analisis Terpadu: menghubungkan Segmentasi Pasar - Forecasting Demand - Performa Rute.

Ketiga modul sebelumnya berjalan sendiri-sendiri. Modul ini menjawab pertanyaan inti penelitian:
"apakah komposisi segmen pelanggan berkaitan dengan permintaan dan performa rute, dan keputusan
operasional apa yang bisa diambil dari ketiganya sekaligus?"

Alur:
  1. Segmentasi  : RFM -> log -> Min-Max -> K-Means (segmentasi_service). Tiap transaksi diberi
                   segmen pelanggannya, sehingga tiap rute punya KOMPOSISI SEGMEN.
  2. Performa    : okupansi (load factor) per rute dan per bulan (performa_service).
  3. Forecasting : prediksi penumpang 30 hari ke depan per rute (forecasting_service) dan
                   pertumbuhannya terhadap 30 hari aktual terakhir.
  4. Integrasi   :
       a. Korelasi Pearson & Spearman pada panel RUTE x BULAN (n = jumlah rute x jumlah bulan)
          antara: persen transaksi segmen inti, rata-rata skor RFM, penumpang (permintaan), dan
          okupansi. Uji signifikansi p < 0,05.
       b. Korelasi indikatif antar-rute (n kecil) antara pertumbuhan hasil forecast dan
          okupansi / komposisi segmen.
       c. Matriks prioritas aksi per rute (aturan okupansi x pertumbuhan forecast x komposisi segmen).
Korelasi bukan bukti sebab-akibat; hasil dibaca sebagai hubungan statistik pada data historis.
"""
import logging
import math
import threading
import warnings

import numpy as np
import pandas as pd
from scipy import stats
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.models import Rute, Transaksi
from app.services import forecasting_service, performa_service, segmentasi_service

log = logging.getLogger(__name__)

ALPHA = 0.05
MIN_N_KORELASI = 8          # minimal pasangan data agar korelasi dilaporkan
HORIZON_HARI = 30
BATAS_OKUPANSI_TINGGI = performa_service.BATAS_TINGGI
BATAS_OKUPANSI_SEDANG = performa_service.BATAS_SEDANG
SELISIH_INTI_BERARTI = 2.0  # poin persen di bawah rata-rata rute yang dianggap berarti
AMBANG_TUMBUH = 5.0         # % pertumbuhan forecast yang dianggap naik/turun berarti

VARIABEL = {
    "persen_inti": "Persen transaksi segmen inti",
    "rata_skor_rfm": "Rata-rata skor RFM",
    "penumpang": "Penumpang (permintaan)",
    "okupansi": "Okupansi (%)",
}

ALUR = [
    {"tahap": "1. Segmentasi Pasar", "metode": "RFM -> log(1+x) -> Min-Max -> K-Means (K = 2..8, dipilih Silhouette tertinggi)",
     "keluaran": "Segmen tiap pelanggan -> komposisi segmen per rute & bulan"},
    {"tahap": "2. Forecasting Demand",
     "metode": "SARIMAX (musiman + kalender, ordo AIC) vs ARIMA baseline; model dengan MAPE terkecil pada 28 hari uji",
     "keluaran": "Prediksi penumpang 30 hari ke depan dan pertumbuhannya terhadap 30 hari aktual terakhir"},
    {"tahap": "3. Performa Rute", "metode": "Statistik deskriptif + load factor (penumpang / kapasitas armada)",
     "keluaran": "Okupansi rata-rata per trip dan status Tinggi / Sedang / Rendah"},
    {"tahap": "4. Integrasi", "metode": "Korelasi Pearson & Spearman (panel rute x bulan) + matriks prioritas aksi",
     "keluaran": "Hubungan antar-modul dan rekomendasi tindakan per rute"},
]

_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAKS = 16


# --------------------------------------------------------------------------------------
# Data panel rute x bulan
# --------------------------------------------------------------------------------------
def _signature(db: Session, cabang: str | None):
    q = db.query(func.count(Transaksi.id_transaksi), func.max(Transaksi.tanggal))
    if cabang:
        q = q.filter(Transaksi.cabang_asal == cabang)
    n, maks = q.one()
    return int(n or 0), str(maks) if maks else None


def _filter(q, cabang):
    q = q.filter(Transaksi.jenis_transaksi == "Penumpang")
    return q.filter(Transaksi.cabang_asal == cabang) if cabang else q


def _transaksi_segmen(db: Session, cabang: str | None, label_pelanggan: dict) -> pd.DataFrame:
    rows = _filter(db.query(Transaksi.id_rute, Transaksi.tanggal, Transaksi.id_pelanggan,
                            Transaksi.jumlah_unit), cabang).all()
    df = pd.DataFrame(rows, columns=["id_rute", "tanggal", "nama_pelanggan", "jumlah_unit"])
    if df.empty:
        return df
    df["bulan"] = pd.to_datetime(df["tanggal"]).dt.to_period("M").astype(str)
    df["segmen"] = df["nama_pelanggan"].map(label_pelanggan)
    return df


def _okupansi_bulanan(db: Session, cabang: str | None) -> pd.DataFrame:
    """Okupansi per (rute, bulan) dengan definisi yang sama seperti performa_service:
    rata-rata (penumpang trip / kapasitas armada) x 100 per trip."""
    rows = _filter(db.query(Transaksi.id_rute, Transaksi.tanggal, Transaksi.layanan, Transaksi.id_armada,
                            func.count(func.distinct(Transaksi.jam_keberangkatan)),
                            func.sum(Transaksi.jumlah_unit), func.sum(Transaksi.total_bayar)), cabang) \
        .group_by(Transaksi.id_rute, Transaksi.tanggal, Transaksi.layanan, Transaksi.id_armada).all()
    df = pd.DataFrame(rows, columns=["id_rute", "tanggal", "layanan", "id_armada", "trip", "penumpang", "pendapatan"])
    if df.empty:
        return df
    per_armada, rata_layanan = performa_service._kapasitas_armada(db)
    kap = [per_armada.get(a) or performa_service._kapasitas_fallback(l, rata_layanan)
           for a, l in zip(df["id_armada"], df["layanan"])]
    df["kapasitas"] = np.asarray(kap, dtype=float)
    df["penumpang"] = df["penumpang"].astype(float)
    df["pendapatan"] = df["pendapatan"].astype(float)
    df["rasio"] = np.where(df["kapasitas"] > 0, df["penumpang"] / df["kapasitas"], 0.0)
    df["bulan"] = pd.to_datetime(df["tanggal"]).dt.to_period("M").astype(str)
    g = df.groupby(["id_rute", "bulan"]).agg(trip=("trip", "sum"), rasio=("rasio", "sum"),
                                              penumpang=("penumpang", "sum"),
                                              pendapatan=("pendapatan", "sum")).reset_index()
    g["okupansi"] = np.where(g["trip"] > 0, 100 * g["rasio"] / g["trip"], np.nan)
    return g[["id_rute", "bulan", "trip", "penumpang", "pendapatan", "okupansi"]]


def _komposisi(df_seg: pd.DataFrame, segmen_urut: list[str], skor_label: dict, jumlah_inti: int):
    """Fitur segmen per (rute, bulan) dan per rute."""
    inti = set(segmen_urut[:jumlah_inti])
    d = df_seg.dropna(subset=["segmen"]).copy()
    d["inti"] = d["segmen"].isin(inti).astype(float)
    d["skor"] = d["segmen"].map(skor_label).astype(float)
    per_bulan = d.groupby(["id_rute", "bulan"]).agg(
        persen_inti=("inti", lambda s: 100 * s.mean()), rata_skor_rfm=("skor", "mean"),
        n_transaksi=("inti", "size")).reset_index()
    per_rute = {}
    for id_rute, sub in d.groupby("id_rute"):
        total = len(sub)
        komp = sub["segmen"].value_counts()
        per_rute[id_rute] = {
            "komposisi": [{"segmen": s, "persen": round(100 * float(komp.get(s, 0)) / total, 1)}
                          for s in segmen_urut],
            "persen_inti": round(100 * float(sub["inti"].mean()), 1),
            "rata_skor_rfm": round(float(sub["skor"].mean()), 3),
            "segmen_dominan": str(komp.idxmax()),
            "persen_pasif": round(100 * float(komp.get(segmen_urut[-1], 0)) / total, 1),
        }
    return per_bulan, per_rute


# --------------------------------------------------------------------------------------
# Korelasi
# --------------------------------------------------------------------------------------
def _kekuatan(r: float) -> str:
    a = abs(r)
    return ("sangat lemah" if a < 0.2 else "lemah" if a < 0.4 else "sedang" if a < 0.6
            else "kuat" if a < 0.8 else "sangat kuat")


def _korelasi_pasangan(x: pd.Series, y: pd.Series) -> dict | None:
    d = pd.concat([x, y], axis=1).dropna()
    if len(d) < MIN_N_KORELASI or d.iloc[:, 0].std() == 0 or d.iloc[:, 1].std() == 0:
        return None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        pr, pp = stats.pearsonr(d.iloc[:, 0], d.iloc[:, 1])
        sr, sp = stats.spearmanr(d.iloc[:, 0], d.iloc[:, 1])
    if any(map(math.isnan, (pr, pp, sr, sp))):
        return None
    return {"n": int(len(d)), "pearson_r": round(float(pr), 3), "pearson_p": round(float(pp), 4),
            "spearman_rho": round(float(sr), 3), "spearman_p": round(float(sp), 4),
            "signifikan": bool(pp < ALPHA), "kekuatan": _kekuatan(pr),
            "arah": "positif" if pr > 0 else "negatif"}


def _hubungan(df: pd.DataFrame, pasangan: list[tuple[str, str, str]]) -> list[dict]:
    hasil = []
    for a, b, tafsir in pasangan:
        k = _korelasi_pasangan(df[a], df[b]) if a in df and b in df else None
        item = {"x": a, "y": b, "label_x": VARIABEL.get(a, a), "label_y": VARIABEL.get(b, b),
                "hipotesis": tafsir, "hasil": k}
        item["kesimpulan"] = _kalimat_hubungan(item)
        hasil.append(item)
    return hasil


def _kalimat_hubungan(h: dict) -> str:
    k = h["hasil"]
    if k is None:
        return "Data belum cukup atau tidak bervariasi untuk menghitung korelasi."
    sig = "signifikan" if k["signifikan"] else "tidak signifikan"
    return (f"Korelasi {k['arah']} {k['kekuatan']} (r = {k['pearson_r']}, p = {k['pearson_p']}, n = {k['n']}); "
            f"secara statistik {sig} pada alpha {ALPHA}.")


def _matriks(df: pd.DataFrame, kolom: list[str]) -> dict:
    d = df[kolom].dropna()
    if len(d) < MIN_N_KORELASI:
        return {"variabel": [], "label": [], "matriks": []}
    ok = [c for c in kolom if d[c].std() > 0]
    m = d[ok].corr(method="pearson").fillna(0.0)
    return {"variabel": ok, "label": [VARIABEL.get(c, c) for c in ok],
            "matriks": [[round(float(v), 3) for v in row] for row in m.to_numpy()]}


# --------------------------------------------------------------------------------------
# Rekomendasi
# --------------------------------------------------------------------------------------
def _rekomendasi(okupansi: float, tumbuh: float | None, seg: dict | None, rata_inti: float | None) -> dict:
    """Matriks prioritas: okupansi (kondisi sekarang) x pertumbuhan forecast (arah ke depan),
    diperkaya komposisi segmen."""
    if tumbuh is None:
        return {"prioritas": "Data belum lengkap", "tindakan": "Forecast belum tersedia untuk rute ini.",
                "tone": "slate"}
    naik, turun = tumbuh >= AMBANG_TUMBUH, tumbuh <= -AMBANG_TUMBUH
    if okupansi >= BATAS_OKUPANSI_TINGGI:
        if not turun:
            hasil = ("Tinggi", "Tambah kapasitas",
                     f"Okupansi {okupansi:.1f}% sudah tinggi dan permintaan diprediksi {tumbuh:+.1f}%: "
                     "tambah armada/jadwal pada jam padat.", "red")
        else:
            hasil = ("Sedang", "Pertahankan & pantau",
                     f"Okupansi tinggi ({okupansi:.1f}%) tetapi permintaan diprediksi turun {tumbuh:+.1f}%: "
                     "jaga layanan, tunda penambahan armada.", "amber")
    elif okupansi >= BATAS_OKUPANSI_SEDANG:
        if naik:
            hasil = ("Sedang", "Siaga tambah jadwal",
                     f"Okupansi {okupansi:.1f}% dan permintaan naik {tumbuh:+.1f}%: siapkan jadwal tambahan "
                     "pada hari puncak.", "amber")
        else:
            hasil = ("Rendah", "Stabil",
                     f"Okupansi {okupansi:.1f}% dengan permintaan relatif stabil ({tumbuh:+.1f}%): "
                     "pertahankan jadwal saat ini.", "green")
    else:
        if turun:
            hasil = ("Tinggi", "Evaluasi rute",
                     f"Okupansi rendah ({okupansi:.1f}%) dan permintaan diprediksi turun {tumbuh:+.1f}%: "
                     "tinjau jumlah trip/jadwal dan efisiensi armada.", "red")
        else:
            hasil = ("Sedang", "Dorong permintaan",
                     f"Okupansi rendah ({okupansi:.1f}%) dengan permintaan {tumbuh:+.1f}%: "
                     "isi kursi kosong lewat promosi dan jadwal yang lebih menarik.", "amber")
    prioritas, aksi, teks, tone = hasil

    # Pengayaan dari komposisi segmen.
    if seg:
        if seg["persen_pasif"] >= 30:
            teks += (f" {seg['persen_pasif']:.0f}% transaksi rute ini berasal dari segmen pasif: "
                     "jalankan promo reaktivasi.")
        elif rata_inti is not None and seg["persen_inti"] < rata_inti - SELISIH_INTI_BERARTI:
            teks += (f" Porsi segmen inti ({seg['persen_inti']:.0f}%) di bawah "
                     f"({rata_inti:.0f}% rata-rata semua rute): perkuat program loyalitas.")
        else:
            teks += f" Segmen inti kuat ({seg['persen_inti']:.0f}% transaksi): pertahankan lewat program loyalitas."
    return {"prioritas": prioritas, "aksi": aksi, "tindakan": teks, "tone": tone}


# --------------------------------------------------------------------------------------
# Forecast ringkas per rute
# --------------------------------------------------------------------------------------
def _forecast_rute(db: Session, nama_rute: str) -> dict | None:
    try:
        f = forecasting_service.jalankan_forecast(db, nama_rute, horizon_hari=HORIZON_HARI, pakai_kalender=True)
    except Exception as e:  # noqa: BLE001 - satu rute gagal tidak boleh menggagalkan seluruh analisis
        log.warning("Forecast %s gagal pada analisis terpadu: %s", nama_rute, e)
        return None
    aktual = sum(d["aktual"] for d in f["deret"] if d.get("aktual") is not None)
    prediksi = float(f["prediksi_periode_berikutnya"])
    tumbuh = round(100 * (prediksi - aktual) / aktual, 1) if aktual else None
    return {"model": f["model"], "mape_persen": f["mape_persen"], "valid": f["valid"],
            "kategori_mape": f["kategori_mape"], "aktual_30_hari": int(round(aktual)),
            "prediksi_30_hari": int(round(prediksi)), "pertumbuhan_persen": tumbuh,
            "rekomendasi_unit_tambahan": f.get("rekomendasi_unit_tambahan")}


# --------------------------------------------------------------------------------------
# Utama
# --------------------------------------------------------------------------------------
def _panel(db: Session, cabang: str | None) -> dict:
    seg = segmentasi_service.jalankan_rfm_kmeans(db, n_clusters=None, cabang=cabang,
                                                 limit_pelanggan=10 ** 9)
    ringkasan = seg.get("ringkasan_cluster") or []
    if not ringkasan:
        raise ValueError(seg.get("pesan") or "Belum ada pelanggan untuk disegmentasi.")
    segmen_urut = [c["label"] for c in ringkasan]  # sudah urut skor tertinggi -> terendah
    skor_label = {c["label"]: c["skor"] for c in ringkasan}
    label_pelanggan = {p["penumpang"]: p["segmen"] for p in seg["pelanggan"]}
    jumlah_inti = max(1, len(segmen_urut) // 2)

    df_seg = _transaksi_segmen(db, cabang, label_pelanggan)
    if df_seg.empty:
        raise ValueError("Belum ada transaksi penumpang untuk dianalisis.")
    seg_bulan, seg_rute = _komposisi(df_seg, segmen_urut, skor_label, jumlah_inti)
    okup = _okupansi_bulanan(db, cabang)
    panel = okup.merge(seg_bulan, on=["id_rute", "bulan"], how="inner")
    panel = panel[panel["trip"] > 0].reset_index(drop=True)

    hubungan = _hubungan(panel, [
        ("persen_inti", "okupansi", "Semakin besar porsi pelanggan inti, semakin tinggi okupansi rute."),
        ("persen_inti", "penumpang", "Semakin besar porsi pelanggan inti, semakin tinggi permintaan penumpang."),
        ("rata_skor_rfm", "okupansi", "Semakin tinggi skor RFM rata-rata pelanggan rute, semakin tinggi okupansi."),
        ("penumpang", "okupansi", "Semakin tinggi permintaan, semakin tinggi okupansi (keterkaitan forecasting-performa)."),
    ])
    return {"seg": seg, "segmen_urut": segmen_urut, "jumlah_inti": jumlah_inti, "seg_rute": seg_rute,
            "panel": panel, "hubungan": hubungan,
            "matriks": _matriks(panel, ["persen_inti", "rata_skor_rfm", "penumpang", "okupansi"])}


def analisis_terpadu(db: Session, cabang: str | None = None, sertakan_forecast: bool = True) -> dict:
    kunci = (cabang, _signature(db, cabang))
    with _CACHE_LOCK:
        p = _CACHE.get(kunci)
    if p is None:
        p = _panel(db, cabang)
        with _CACHE_LOCK:
            if len(_CACHE) >= _CACHE_MAKS:
                _CACHE.pop(next(iter(_CACHE)))
            _CACHE[kunci] = p

    seg, panel = p["seg"], p["panel"]
    performa = performa_service.analisis_performa_rute(db, cabang)
    id_ke_nama = dict(db.query(Rute.id_rute, Rute.nama_rute).all())
    rata_inti = (float(np.mean([v["persen_inti"] for v in p["seg_rute"].values()]))
                 if p["seg_rute"] else None)

    rute = []
    for perf in performa:
        seg_r = p["seg_rute"].get(perf["id_rute"])
        fc = _forecast_rute(db, perf["rute"]) if sertakan_forecast else None
        rek = _rekomendasi(perf["okupansi_persen"], fc["pertumbuhan_persen"] if fc else None, seg_r, rata_inti)
        rute.append({
            "id_rute": perf["id_rute"], "rute": perf["rute"], "cabang_asal": perf["cabang_asal"],
            "segmentasi": seg_r,
            "forecasting": fc,
            "performa": {"total_trip": perf["total_trip"], "total_penumpang": perf["total_penumpang"],
                         "total_pendapatan": perf["total_pendapatan"],
                         "okupansi_persen": perf["okupansi_persen"], "status": perf["status"],
                         "layanan_dominan": perf["layanan_dominan"]},
            "rekomendasi": rek,
        })

    # Korelasi indikatif antar-rute (n kecil).
    df_rute = pd.DataFrame([{
        "pertumbuhan": r["forecasting"]["pertumbuhan_persen"] if r["forecasting"] else np.nan,
        "okupansi": r["performa"]["okupansi_persen"],
        "persen_inti": r["segmentasi"]["persen_inti"] if r["segmentasi"] else np.nan,
    } for r in rute])
    antar_rute = []
    for a, b, ket in (("pertumbuhan", "okupansi", "Pertumbuhan hasil forecast vs okupansi saat ini"),
                      ("pertumbuhan", "persen_inti", "Pertumbuhan hasil forecast vs porsi segmen inti")):
        if len(df_rute) >= 3 and df_rute[a].notna().sum() >= 3:
            d = df_rute[[a, b]].dropna()
            if len(d) >= 3 and d[a].std() > 0 and d[b].std() > 0:
                rho, pv = stats.spearmanr(d[a], d[b])
                antar_rute.append({"keterangan": ket, "n": int(len(d)), "spearman_rho": round(float(rho), 3),
                                   "spearman_p": round(float(pv), 4),
                                   "catatan": "Indikatif saja: jumlah rute sangat sedikit."})
                continue
        antar_rute.append({"keterangan": ket, "n": int(len(df_rute)), "spearman_rho": None,
                           "spearman_p": None, "catatan": "Data belum cukup."})

    ev = seg.get("evaluasi", {})
    ada_sig = [h for h in p["hubungan"] if h["hasil"] and h["hasil"]["signifikan"]]
    if ada_sig:
        kalimat = "; ".join(f"{h['label_x']} dengan {h['label_y']} ({h['hasil']['arah']} {h['hasil']['kekuatan']}, "
                            f"r = {h['hasil']['pearson_r']})" for h in ada_sig)
        kesimpulan = f"Hubungan yang signifikan pada alpha {ALPHA}: {kalimat}."
    else:
        kesimpulan = ("Tidak ada hubungan yang signifikan secara statistik antara komposisi segmen pelanggan "
                      f"dan okupansi/permintaan rute pada alpha {ALPHA}.")
    return {
        "alur": ALUR,
        "segmentasi": {"k_terpilih": ev.get("k_terpilih"), "silhouette": ev.get("silhouette"),
                       "valid": ev.get("valid"), "segmen_urut": p["segmen_urut"],
                       "segmen_inti": p["segmen_urut"][:p["jumlah_inti"]],
                       "jumlah_pelanggan": seg.get("jumlah_pelanggan")},
        "rute": rute,
        "hubungan": p["hubungan"],
        "matriks_korelasi": p["matriks"],
        "antar_rute": antar_rute,
        "kesimpulan": kesimpulan,
        "panel": {"jumlah_observasi": int(len(panel)), "jumlah_rute": int(panel["id_rute"].nunique()),
                  "jumlah_bulan": int(panel["bulan"].nunique()),
                  "titik": [{"rute": id_ke_nama.get(r.id_rute, r.id_rute), "bulan": r.bulan,
                             "persen_inti": round(float(r.persen_inti), 1), "okupansi": round(float(r.okupansi), 1),
                             "penumpang": int(r.penumpang)} for r in panel.itertuples()]},
        "parameter": {"alpha": ALPHA, "horizon_hari": HORIZON_HARI, "ambang_tumbuh_persen": AMBANG_TUMBUH,
                      "batas_okupansi_tinggi": BATAS_OKUPANSI_TINGGI, "batas_okupansi_sedang": BATAS_OKUPANSI_SEDANG},
    }

