"""
Service Forecasting Demand per Rute: perbandingan SARIMA dan SARIMAX (kalender), lalu dipilih
model dengan galat terkecil pada data uji (default: WAPE). Sesuai rancangan algoritma
(P.4 pada DFD) dan BAB II subbab 2.8:
  1. deret harian jumlah penumpang per rute (hanya transaksi 'Penumpang', hari kosong = 0)
     sampai kemarin (WIB): hari ini belum lengkap sehingga tidak dipakai
  2. uji stasioneritas ADF pada data latih fold pertama -> menentukan d (differencing maks. 2 kali)
  3. kandidat model:
       - SARIMAX(p,d,q)(P,D,Q,7)+kalender : musiman mingguan + dummy kalender (akhir pekan,
                                 libur nasional, H-1/H+1 libur nasional, libur sekolah)
       - SARIMA(p,d,q)(P,D,Q,7) : musiman mingguan, tanpa variabel eksogen
     Ordo non-musiman dan musiman dipilih dengan AIC pada data latih fold pertama, lalu dipakai
     tetap pada fold lain.
  4. evaluasi dengan rolling-origin cross-validation (expanding window): 3 lipatan uji
     berukuran 28 hari berurutan di akhir deret. Metrik (MAE, RMSE, MAPE, WAPE) dihitung dari
     gabungan semua lipatan uji; pemenang = galat terkecil menurut KRITERIA_PEMILIHAN
     (diagnostic checking: uji Ljung-Box lag 14 pada residual model terpilih)
  5. model pemenang dilatih ulang pada seluruh data lalu forecast ke depan
  6. MAPE diklasifikasikan menurut Lewis (1982)
"""
import copy
import itertools
import logging
import math
import threading
import time
import warnings
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
from sqlalchemy import func
from sqlalchemy.orm import Session
from statsmodels.tsa.statespace.sarimax import SARIMAX
from statsmodels.tsa.stattools import adfuller

from app.models.models import Armada, Kalender, Rute, Transaksi

log = logging.getLogger(__name__)

ALPHA_ADF = 0.05
MAKS_DIFFERENCING = 2
KANDIDAT_P = range(0, 3)
KANDIDAT_Q = range(0, 3)

MUSIM = 7  # musiman mingguan (data harian)
# Grid SARIMA/SARIMAX: ordo non-musiman (p,d,q) dari grid 0-2, D=1, dan (P,Q) dari grid kecil; dipilih AIC.
KANDIDAT_MUSIMAN_PQ = [(0, 1), (1, 1)]
MAKS_ITERASI = 50  # batas iterasi optimizer state-space (cukup untuk konvergen pada data ini)

# Rolling-origin CV (expanding window): JUMLAH_FOLD lipatan uji berurutan di akhir deret,
# masing-masing HARI_UJI hari (4 siklus mingguan penuh, mendekati horizon 30 hari).
# Data latih selalu mulai dari awal dan bertambah; minimal MIN_LATIH_AWAL hari untuk fold pertama.
HARI_UJI = 28
JUMLAH_FOLD = 3
MIN_LATIH_AWAL = 120
# Jendela latih dibatasi 730 hari terakhir (2 tahun) agar satu kali run tetap terkendali.
JENDELA_LATIH_HARI = 730
MIN_HARI_DATA = 60
HARI_TAMPIL_AKTUAL = 30

# Kapasitas: rata-rata perjalanan/hari dan kapasitas armada dihitung dari 90 hari terakhir.
JENDELA_KAPASITAS_HARI = 90
TARGET_LOAD_FACTOR = 0.80
KAPASITAS_DEFAULT = 10  # bila armada transaksi tidak tercatat

# Kriteria pemilih model & ambang "Valid". WAPE lebih adil untuk rute bervolume kecil.
# Ganti ke "mape" untuk kembali ke perilaku lama.
KRITERIA_PEMILIHAN = "wape"  # "wape" | "mape"
KRITERIA_VALID = "wape"      # "wape" | "mape"
MAPE_MAKS_VALID = 20.0
WAPE_MAKS_VALID = 20.0

# Deret dipotong sampai kemarin (WIB). Transaksi hari ini belum lengkap; bila ikut masuk data uji,
# hari itu terbaca seolah permintaan anjlok.
WIB = timezone(timedelta(hours=7))
# Hari terakhir yang volumenya < 25% median 7 hari sebelumnya juga dianggap belum lengkap
# (misalnya data hari itu baru sebagian diinput/diimpor).
RASIO_HARI_PARSIAL = 0.25
KOLOM_EKSOGEN = ["weekend", "libur_nasional", "sekitar_libur", "libur_sekolah"]

NAMA_HARI = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]

# Cache hasil di memori: key = (rute, horizon, pakai_kalender, tanggal maks, jumlah baris).
# Data baru (tanggal maks / jumlah baris berubah) otomatis membuat key baru.
_CACHE: dict[tuple, dict] = {}
_CACHE_MAKS = 64
_CACHE_LOCK = threading.Lock()


# --------------------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------------------
def _hari_terakhir_lengkap():
    return datetime.now(WIB).date() - timedelta(days=1)


def _signature_rute(db: Session, nama_rute: str):
    return (db.query(func.max(Transaksi.tanggal), func.count(Transaksi.id_transaksi))
              .join(Rute, Rute.id_rute == Transaksi.id_rute)
              .filter(Rute.nama_rute == nama_rute, Transaksi.jenis_transaksi == "Penumpang",
                      Transaksi.tanggal <= _hari_terakhir_lengkap())
              .one())


def _ambil_deret_harian(db: Session, nama_rute: str, tanggal_maks) -> pd.Series:
    # Ambil satu hari lebih, supaya jendela tetap 730 hari bila hari terakhir dibuang (parsial).
    mulai = tanggal_maks - timedelta(days=JENDELA_LATIH_HARI)
    rows = (db.query(Transaksi.tanggal, func.sum(Transaksi.jumlah_unit))
              .join(Rute, Rute.id_rute == Transaksi.id_rute)
              .filter(Rute.nama_rute == nama_rute,
                      Transaksi.jenis_transaksi == "Penumpang",
                      Transaksi.tanggal >= mulai, Transaksi.tanggal <= tanggal_maks)
              .group_by(Transaksi.tanggal)
              .all())
    df = pd.DataFrame(rows, columns=["tanggal", "jumlah_unit"])
    df["tanggal"] = pd.to_datetime(df["tanggal"])
    df["jumlah_unit"] = df["jumlah_unit"].astype(float)
    harian = df.groupby("tanggal")["jumlah_unit"].sum().asfreq("D").fillna(0.0)
    return harian


def _buang_hari_parsial(harian: pd.Series) -> pd.Series:
    if len(harian) > 7:
        acuan = float(harian.iloc[-8:-1].median())
        if acuan > 0 and harian.iloc[-1] < RASIO_HARI_PARSIAL * acuan:
            return harian.iloc[:-1]
    return harian


def _ambil_kalender(db: Session, mulai, selesai) -> pd.DataFrame:
    """Variabel eksogen per tanggal. Tanggal yang belum ada di tabel kalender diisi 0,
    kecuali 'weekend' yang bisa diturunkan langsung dari nama hari."""
    rows = (db.query(Kalender.tanggal, Kalender.weekend, Kalender.libur_nasional,
                     Kalender.libur_sekolah)
              .filter(Kalender.tanggal >= mulai - timedelta(days=1),
                      Kalender.tanggal <= selesai + timedelta(days=1))
              .all())
    idx = pd.date_range(mulai - timedelta(days=1), selesai + timedelta(days=1), freq="D")
    kal = pd.DataFrame(rows, columns=["tanggal", "weekend", "libur_nasional", "libur_sekolah"])
    kal["tanggal"] = pd.to_datetime(kal["tanggal"])
    kal = kal.set_index("tanggal").reindex(idx)
    kal["weekend"] = kal["weekend"].fillna(pd.Series(idx.dayofweek >= 5, index=idx))
    kal = kal.fillna(False).astype(float)
    # Lonjakan penumpang di data terjadi H-1 s.d. H+1 libur nasional (arus mudik/balik),
    # jadi hari di sekitar libur nasional dijadikan dummy tersendiri.
    ln = kal["libur_nasional"]
    sekitar = (ln.shift(1, fill_value=0) + ln.shift(-1, fill_value=0)) > 0
    kal["sekitar_libur"] = (sekitar & (ln == 0)).astype(float)
    return kal.loc[pd.Timestamp(mulai):pd.Timestamp(selesai), KOLOM_EKSOGEN]


def _kapasitas_rute(db: Session, nama_rute: str, tanggal_maks) -> dict:
    """Rata-rata perjalanan per hari (kombinasi jam keberangkatan + layanan + armada) dan
    rata-rata kapasitas armada pada rute ini, 90 hari terakhir."""
    mulai = tanggal_maks - timedelta(days=JENDELA_KAPASITAS_HARI - 1)
    rows = (db.query(Transaksi.tanggal, Transaksi.jam_keberangkatan, Transaksi.layanan,
                     Transaksi.id_armada, func.max(Armada.kapasitas))
              .join(Rute, Rute.id_rute == Transaksi.id_rute)
              .outerjoin(Armada, Armada.id_armada == Transaksi.id_armada)
              .filter(Rute.nama_rute == nama_rute,
                      Transaksi.jenis_transaksi == "Penumpang",
                      Transaksi.tanggal >= mulai, Transaksi.tanggal <= tanggal_maks)
              .group_by(Transaksi.tanggal, Transaksi.jam_keberangkatan, Transaksi.layanan,
                        Transaksi.id_armada)
              .all())
    if not rows:
        return {"perjalanan_per_hari": 0.0, "kapasitas_per_perjalanan": float(KAPASITAS_DEFAULT),
                "kursi_per_hari": 0.0}
    df = pd.DataFrame(rows, columns=["tanggal", "jam", "layanan", "armada", "kapasitas"])
    jumlah_hari = df["tanggal"].nunique()
    perjalanan = len(df) / max(jumlah_hari, 1)
    kap = df["kapasitas"].dropna()
    kap_rata = float(kap.mean()) if len(kap) else float(KAPASITAS_DEFAULT)
    return {"perjalanan_per_hari": perjalanan, "kapasitas_per_perjalanan": kap_rata,
            "kursi_per_hari": perjalanan * kap_rata}


# --------------------------------------------------------------------------------------
# Split data: rolling-origin (expanding window)
# --------------------------------------------------------------------------------------
def _lipatan(n: int) -> list[tuple[int, int]]:
    """Daftar (akhir_latih, akhir_uji) dari yang tertua ke terbaru. Latih = iloc[:a],
    uji = iloc[a:b]. Bila data pendek, jumlah lipatan dikurangi; bila masih kurang,
    dipakai satu lipatan berukuran seperlima data."""
    h, k = HARI_UJI, JUMLAH_FOLD
    while k > 1 and n - h * k < MIN_LATIH_AWAL:
        k -= 1
    if n - h * k < MIN_LATIH_AWAL:
        h, k = max(7, n // 5), 1
    return [(n - h * (i + 1), n - h * i) for i in range(k - 1, -1, -1)]


# --------------------------------------------------------------------------------------
# Model: SARIMA / SARIMAX
# --------------------------------------------------------------------------------------
def _tentukan_d(deret: pd.Series) -> tuple[int, float]:
    """Uji ADF berulang: H0 = deret punya unit root (tidak stasioner).
    Jika p-value >= 0.05, deret di-differencing lalu diuji ulang."""
    y = deret.astype(float)
    p_value = 1.0
    for d in range(MAKS_DIFFERENCING + 1):
        if y.nunique() <= 1:  # deret konstan, ADF tidak bisa dihitung
            return d, 0.0
        p_value = float(adfuller(y, autolag="AIC")[1])
        if p_value < ALPHA_ADF or d == MAKS_DIFFERENCING:
            return d, p_value
        y = y.diff().dropna()
    return MAKS_DIFFERENCING, p_value


def _diam(fn):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # ConvergenceWarning/ValueWarning dari statsmodels
        return fn()


def _fit_sarimax(deret, order, seasonal, exog=None):
    """Dengan exog -> SARIMAX+kalender; tanpa exog -> SARIMA."""
    return _diam(lambda: SARIMAX(deret, exog=exog, order=order, seasonal_order=seasonal,
                                 enforce_stationarity=False, enforce_invertibility=False)
                 .fit(disp=False, maxiter=MAKS_ITERASI))


def _pilih_musiman(deret: pd.Series, d: int, exog=None):
    """Grid ordo non-musiman (p,d,q) x musiman (P,1,Q,7); pilih AIC terkecil.
    Mengembalikan juga model terbaik supaya tidak perlu fit ulang untuk fold pertama."""
    terbaik_order, terbaik_seasonal = None, None
    aic_terbaik, model_terbaik = np.inf, None
    for p, q in itertools.product(KANDIDAT_P, KANDIDAT_Q):
        for P, Q in KANDIDAT_MUSIMAN_PQ:
            seasonal = (P, 1, Q, MUSIM)
            try:
                model = _fit_sarimax(deret, (p, d, q), seasonal, exog)
            except Exception:
                continue
            if np.isfinite(model.aic) and model.aic < aic_terbaik:
                terbaik_order, terbaik_seasonal = (p, d, q), seasonal
                aic_terbaik, model_terbaik = model.aic, model
    return terbaik_order, terbaik_seasonal, model_terbaik


def _buat_final(harian, exog_all, exog_depan, order, seasonal, kolom):
    """Latih ulang pada seluruh data, forecast hh hari. Mengembalikan (forecast, model)."""
    def final(hh):
        X = exog_all[kolom] if kolom else None
        Xd = exog_depan[kolom].iloc[:hh] if kolom else None
        m = _fit_sarimax(harian, order, seasonal, X)
        return np.asarray(m.forecast(hh, exog=Xd), dtype=float), m
    return final


# --------------------------------------------------------------------------------------
# Evaluasi
# --------------------------------------------------------------------------------------
def _metrik(aktual: np.ndarray, prediksi: np.ndarray) -> dict:
    galat = aktual - prediksi
    mae = float(np.mean(np.abs(galat)))
    rmse = float(np.sqrt(np.mean(galat ** 2)))
    # MAPE tidak terdefinisi saat aktual = 0, jadi hari tanpa penumpang dikeluarkan.
    nonzero = aktual != 0
    mape = float(np.mean(np.abs(galat[nonzero] / aktual[nonzero])) * 100) if nonzero.any() else 0.0
    # WAPE = total |galat| / total aktual: stabil untuk rute bervolume kecil.
    total = float(np.sum(aktual))
    wape = float(np.sum(np.abs(galat)) / total * 100) if total > 0 else 0.0
    return {"mae": mae, "rmse": rmse, "mape": mape, "wape": wape}


LAG_LJUNG_BOX = 14  # dua siklus mingguan
ALPHA_LJUNG_BOX = 0.05


def _uji_residual(model) -> dict | None:
    """Diagnostic checking: uji Ljung-Box pada residual model terpilih.
    H0 = residual tidak berautokorelasi (white noise). p-value > 0,05 berarti H0 tidak ditolak,
    sehingga pola data dianggap sudah tertangkap model. Residual awal (masa pemanasan
    differencing/musiman) dibuang agar tidak mendistorsi uji."""
    try:
        from statsmodels.stats.diagnostic import acorr_ljungbox
        resid = pd.Series(np.asarray(model.resid, dtype=float)).iloc[2 * MUSIM:]
        resid = resid[np.isfinite(resid)]
        if len(resid) <= LAG_LJUNG_BOX * 2:
            return None
        p_value = float(acorr_ljungbox(resid, lags=[LAG_LJUNG_BOX], return_df=True)["lb_pvalue"].iloc[0])
        return {"metode": "Ljung-Box", "lag": LAG_LJUNG_BOX, "p_value": round(p_value, 4),
                "lolos": bool(p_value > ALPHA_LJUNG_BOX)}
    except Exception:
        return None


def kategori_mape(mape: float) -> str:
    """Klasifikasi akurasi peramalan menurut Lewis (1982)."""
    if mape < 10:
        return "Sangat baik"
    if mape <= 20:
        return "Baik"
    if mape <= 50:
        return "Layak"
    return "Buruk"


def _bandingkan_model(harian, exog_all, exog_depan, folds, d, pakai_kalender=False):
    """Evaluasi SARIMAX+kalender dan SARIMA dengan rolling-origin CV. Metrik dihitung dari
    gabungan semua lipatan uji. Hasil: list dict {nama, metrik, pakai_exog,
    final(h) -> (forecast ndarray, model statsmodels)}."""
    a0 = folds[0][0]
    aktual = np.concatenate([harian.iloc[a:b].values for a, b in folds]).astype(float)
    kandidat = []

    def catat(nama, pred, final, pakai_exog=None):
        pred = np.clip(np.asarray(pred, dtype=float), 0, None)
        if not np.all(np.isfinite(pred)):
            return None
        k = {"nama": nama, "metrik": _metrik(aktual, pred), "pred": pred,
             "final": final, "pakai_exog": pakai_exog or []}
        kandidat.append(k)
        return k

    # Ordo dipilih AIC pada latih fold pertama, lalu dipakai tetap pada fold lain (hemat waktu).
    def evaluasi(kolom):
        train0 = harian.iloc[:a0]
        order, seasonal, model0 = _pilih_musiman(
            train0, d, exog_all[kolom].iloc[:a0] if kolom else None)
        if model0 is None:
            return None
        preds = []
        for a, b in folds:
            if a == a0:
                m = model0
            else:
                m = _fit_sarimax(harian.iloc[:a], order, seasonal,
                                 exog_all[kolom].iloc[:a] if kolom else None)
            preds.append(np.asarray(
                m.forecast(b - a, exog=exog_all[kolom].iloc[a:b] if kolom else None), dtype=float))
        return order, seasonal, np.concatenate(preds)

    # 1) SARIMAX: musiman mingguan + dummy kalender
    if exog_all is not None:
        kolom = [c for c in exog_all.columns if exog_all[c].iloc[:a0].nunique() > 1]
        if kolom:
            try:
                hasil = evaluasi(kolom)
                if hasil:
                    order, seasonal, pred = hasil
                    p, _, q = order
                    P, D, Q, s = seasonal
                    catat(f"SARIMAX({p},{d},{q})({P},{D},{Q},{s})+kalender", pred,
                          _buat_final(harian, exog_all, exog_depan, order, seasonal, kolom),
                          pakai_exog=kolom)
            except Exception:
                log.warning("SARIMAX gagal", exc_info=True)

    # 2) SARIMA: musiman mingguan, tanpa eksogen
    try:
        hasil = evaluasi(None)
        if hasil:
            order, seasonal, pred = hasil
            p, _, q = order
            P, D, Q, s = seasonal
            catat(f"SARIMA({p},{d},{q})({P},{D},{Q},{s})", pred,
                  _buat_final(harian, exog_all, exog_depan, order, seasonal, None))
    except Exception:
        log.warning("SARIMA gagal", exc_info=True)

    # Matriks korelasi (heatmap) dihitung dari data latih fold pertama + variabel eksogen
    if pakai_kalender and exog_all is not None:
        try:
            df_k = pd.concat([harian.iloc[:a0].rename("penumpang"), exog_all.iloc[:a0]], axis=1).dropna()
            if not df_k.empty and len(df_k) > 1:
                matriks = df_k.corr().round(3)
                for k in kandidat:
                    k["matrix_korelasi"] = {str(a): {str(b): float(matriks.loc[a, b])
                                                      for b in matriks.columns}
                                              for a in matriks.index}
        except Exception:
            pass

    return kandidat


# --------------------------------------------------------------------------------------
# Rekomendasi
# --------------------------------------------------------------------------------------
def _rekomendasi(forecast: pd.Series, kapasitas: dict) -> dict:
    puncak_tgl = forecast.idxmax()
    puncak = float(forecast.max())
    rata = float(forecast.mean())
    kursi = kapasitas["kursi_per_hari"]
    kap_perjalanan = max(kapasitas["kapasitas_per_perjalanan"], 1.0)
    perjalanan_now = kapasitas["perjalanan_per_hari"]

    # Perjalanan yang dibutuhkan agar load factor hari puncak tidak melebihi 80%.
    butuh = math.ceil(puncak / (kap_perjalanan * TARGET_LOAD_FACTOR)) if puncak > 0 else 0
    tambahan = max(0, butuh - math.floor(perjalanan_now + 1e-9))
    hari_tambahan = int((forecast > kursi * TARGET_LOAD_FACTOR).sum()) if kursi > 0 else 0

    # Hari tersibuk menurut rata-rata prediksi per nama hari.
    per_hari = forecast.groupby(forecast.index.dayofweek).mean().sort_values(ascending=False)
    sibuk = [NAMA_HARI[i] for i in per_hari.index[:2]]

    lf_puncak = puncak / kursi * 100 if kursi > 0 else 0.0
    lf_rata = rata / kursi * 100 if kursi > 0 else 0.0
    catatan = (f"Permintaan tertinggi diperkirakan pada hari {' dan '.join(sibuk)}; "
               f"puncak {puncak:.0f} pax pada {puncak_tgl.strftime('%d-%m-%Y')} "
               f"(load factor {lf_puncak:.0f}% dari {kursi:.0f} kursi/hari). ")
    if tambahan > 0:
        catatan += (f"Tambahkan {tambahan} perjalanan/unit pada {hari_tambahan} hari "
                    f"dengan prediksi di atas {TARGET_LOAD_FACTOR:.0%} kapasitas.")
    else:
        catatan += "Kapasitas jadwal saat ini mencukupi, tidak perlu unit tambahan."

    return {
        "rekomendasi_unit_tambahan": int(tambahan),
        "catatan_jadwal": catatan,
        "kapasitas": {
            "perjalanan_per_hari": round(perjalanan_now, 1),
            "kapasitas_per_perjalanan": round(kap_perjalanan, 1),
            "kursi_per_hari": round(kursi, 0),
            "target_load_factor_persen": round(TARGET_LOAD_FACTOR * 100, 0),
            "prediksi_puncak_harian": round(puncak, 0),
            "tanggal_puncak": puncak_tgl.date(),
            "prediksi_rata_harian": round(rata, 1),
            "load_factor_puncak_persen": round(lf_puncak, 1),
            "load_factor_rata_persen": round(lf_rata, 1),
            "hari_perlu_tambahan": hari_tambahan,
        },
    }


# --------------------------------------------------------------------------------------
# Public
# --------------------------------------------------------------------------------------
def jalankan_forecast(db: Session, nama_rute: str, horizon_hari: int = 30,
                      pakai_kalender: bool = True) -> dict:
    tanggal_maks, jumlah_baris = _signature_rute(db, nama_rute)
    if tanggal_maks is None:
        raise ValueError(f"Tidak ada data penumpang untuk rute '{nama_rute}'.")
    if isinstance(tanggal_maks, str):  # jaga-jaga driver mengembalikan string
        tanggal_maks = pd.Timestamp(tanggal_maks).date()

    key = (nama_rute, int(horizon_hari), bool(pakai_kalender), str(tanggal_maks), int(jumlah_baris))
    with _CACHE_LOCK:
        if key in _CACHE:
            hasil = copy.deepcopy(_CACHE[key])
            hasil["dari_cache"] = True
            return hasil

    mulai_waktu = time.perf_counter()
    harian = _buang_hari_parsial(_ambil_deret_harian(db, nama_rute, tanggal_maks))
    harian = harian.iloc[-JENDELA_LATIH_HARI:]
    if len(harian) < MIN_HARI_DATA:
        raise ValueError(f"Data historis rute ini belum cukup untuk forecasting "
                         f"(minimal {MIN_HARI_DATA} hari).")

    idx_depan = pd.date_range(harian.index[-1] + pd.Timedelta(days=1), periods=horizon_hari,
                              freq="D")
    exog_all = exog_depan = None
    if pakai_kalender:
        kal = _ambil_kalender(db, harian.index[0].date(), idx_depan[-1].date())
        exog_all, exog_depan = kal.loc[harian.index], kal.loc[idx_depan]

    # --- Evaluasi akurasi: rolling-origin CV (expanding window) ---
    folds = _lipatan(len(harian))
    d, adf_p_value = _tentukan_d(harian.iloc[:folds[0][0]])

    kandidat = _bandingkan_model(harian, exog_all, exog_depan, folds, d,
                                 pakai_kalender=pakai_kalender)
    if not kandidat:
        raise ValueError("Semua model gagal dilatih untuk rute ini.")
    pemenang = min(kandidat, key=lambda k: k["metrik"][KRITERIA_PEMILIHAN])

    # --- Model pemenang dilatih ulang pada seluruh data, lalu forecast ke depan ---
    fc, model_final = pemenang["final"](horizon_hari)
    forecast = pd.Series(np.clip(np.asarray(fc, dtype=float), 0, None), index=idx_depan)

    metrik = pemenang["metrik"]
    mape = metrik["mape"]
    wape = metrik["wape"]
    akurasi = max(0.0, 100 - (wape if KRITERIA_VALID == "wape" else mape))

    deret = []
    for tgl, val in harian.iloc[-HARI_TAMPIL_AKTUAL:].items():
        deret.append({"tanggal": tgl.date(), "aktual": round(float(val), 1), "prediksi": None})
    for tgl, val in forecast.items():
        deret.append({"tanggal": tgl.date(), "aktual": None, "prediksi": round(float(val), 1)})

    perbandingan = [{
        "model": k["nama"],
        "mae": round(k["metrik"]["mae"], 2),
        "rmse": round(k["metrik"]["rmse"], 2),
        "mape_persen": round(k["metrik"]["mape"], 2),
        "wape_persen": round(k["metrik"]["wape"], 2),
        "kategori_mape": kategori_mape(k["metrik"]["mape"]),
        "terpilih": k is pemenang,
        "matrix_korelasi": k.get("matrix_korelasi"),
    } for k in sorted(kandidat, key=lambda k: k["metrik"][KRITERIA_PEMILIHAN])]

    # Matriks korelasi antar variabel eksogen (heatmap) + korelasi dengan jumlah penumpang.
    korelasi = None
    if pakai_kalender and exog_all is not None:
        try:
            df_korelasi = pd.concat([harian.rename("penumpang"), exog_all], axis=1).dropna()
            if not df_korelasi.empty and len(df_korelasi) > 1:
                korelasi = {str(c): round(float(df_korelasi[c].corr(df_korelasi["penumpang"])), 4)
                            for c in df_korelasi.columns if c != "penumpang"}
                matriks = df_korelasi.corr().round(3)
                korelasi["matriks"] = {str(a): {str(b): float(matriks.loc[a, b])
                                                for b in matriks.columns}
                                       for a in matriks.index}
        except Exception:
            pass

    rek = _rekomendasi(forecast, _kapasitas_rute(db, nama_rute, harian.index[-1].date()))

    ambang_valid = WAPE_MAKS_VALID if KRITERIA_VALID == "wape" else MAPE_MAKS_VALID
    hasil = {
        "rute": nama_rute,
        "model": pemenang["nama"],
        "adf_p_value": round(adf_p_value, 4),
        "ordo_differencing": d,
        "prediksi_periode_berikutnya": round(float(forecast.sum()), 0),
        "horizon_hari": int(horizon_hari),
        "akurasi_persen": round(akurasi, 1),
        "mape_persen": round(mape, 2),
        "wape_persen": round(wape, 2),
        "mae": round(metrik["mae"], 2),
        "rmse": round(metrik["rmse"], 2),
        "kategori_mape": kategori_mape(mape),
        "valid": bool(metrik[KRITERIA_VALID] <= ambang_valid),
        "kriteria_pemilihan": KRITERIA_PEMILIHAN,
        "kriteria_valid": KRITERIA_VALID,
        "mape_maks_valid": MAPE_MAKS_VALID,
        "wape_maks_valid": WAPE_MAKS_VALID,
        "perbandingan": perbandingan,
        "pakai_kalender": bool(pakai_kalender),
        "variabel_eksogen": pemenang["pakai_exog"] or [],
        "korelasi": korelasi,
        "metode_evaluasi": "Rolling-origin CV (expanding window)",
        "jumlah_fold": len(folds),
        "hari_per_fold": int(folds[0][1] - folds[0][0]),
        "hari_uji": int(sum(b - a for a, b in folds)),
        "jendela_latih_hari": int(len(harian)),
        "periode_data": {"mulai": harian.index[0].date(), "selesai": harian.index[-1].date()},
        "rekomendasi_unit_tambahan": rek["rekomendasi_unit_tambahan"],
        "catatan_jadwal": rek["catatan_jadwal"],
        "kapasitas": rek["kapasitas"],
        "uji_residual": _uji_residual(model_final),
        "deret": deret,
        "durasi_detik": round(time.perf_counter() - mulai_waktu, 1),
        "dari_cache": False,
    }

    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAKS:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = copy.deepcopy(hasil)
    return hasil