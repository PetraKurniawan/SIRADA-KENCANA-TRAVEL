"""
Service analisis performa rute berbasis Machine Learning.
Unit analisis: 1 trip = (tanggal, jam_keberangkatan, id_rute, layanan, id_armada).
Target: okupansi trip = total_penumpang / kapasitas_armada.
"""
from datetime import date, timedelta
from typing import Optional, Dict, Any, List
import threading
import time
import pandas as pd
import numpy as np

from sqlalchemy.orm import Session
from sqlalchemy import func

from sklearn.ensemble import HistGradientBoostingRegressor, IsolationForest
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.inspection import permutation_importance
from sklearn.preprocessing import OneHotEncoder
from sklearn.compose import ColumnTransformer
from sklearn.pipeline import Pipeline

from app.models.models import Transaksi, Kalender, Rute, Jadwal
from app.services.performa_service import _kapasitas_armada, _kapasitas_fallback, _sidik_data

_cache_lock = threading.Lock()
_cache: Dict[tuple, tuple] = {}
_CACHE_TTL_DETIK = 3600  # 1 jam


def _koreksi_sekitar_libur(df_kalender: pd.DataFrame) -> pd.Series:
    """True bila H-1 atau H+1 libur nasional."""
    libur = df_kalender["libur_nasional"].fillna(False)
    prev_libur = libur.shift(-1).fillna(False)
    next_libur = libur.shift(1).fillna(False)
    return (prev_libur | next_libur) & (~libur)


def _siapkan_data_trip(db: Session, cabang: Optional[str] = None):
    """Ambil data transaksi penumpang dan bentuk unit trip."""
    per_armada, rata_layanan = _kapasitas_armada(db)

    # 1. Agregasi penumpang per trip
    q = (db.query(
            Transaksi.tanggal,
            Transaksi.jam_keberangkatan,
            Transaksi.id_rute,
            Rute.nama_rute,
            Transaksi.layanan,
            Transaksi.id_armada,
            Transaksi.cabang_asal,
            func.coalesce(func.sum(Transaksi.jumlah_unit), 0).label("penumpang")
        )
        .join(Rute, Transaksi.id_rute == Rute.id_rute)
        .filter(Transaksi.jenis_transaksi == "Penumpang"))

    if cabang:
        q = q.filter(Transaksi.cabang_asal == cabang)

    q = q.group_by(
        Transaksi.tanggal,
        Transaksi.jam_keberangkatan,
        Transaksi.id_rute,
        Rute.nama_rute,
        Transaksi.layanan,
        Transaksi.id_armada,
        Transaksi.cabang_asal
    )

    rows = q.all()
    if not rows:
        return pd.DataFrame(), pd.DataFrame()

    trip_records = []
    for r in rows:
        kap = per_armada.get(r.id_armada) or _kapasitas_fallback(r.layanan, rata_layanan)
        kap = float(kap) if kap and kap > 0 else 10.0
        pnp = float(r.penumpang)
        okupansi = min(1.0, max(0.0, pnp / kap))
        trip_records.append({
            "tanggal": r.tanggal,
            "jam": str(r.jam_keberangkatan).strip(),
            "id_rute": r.id_rute,
            "nama_rute": r.nama_rute,
            "layanan": r.layanan,
            "penumpang": pnp,
            "kapasitas": kap,
            "okupansi": okupansi
        })

    df_trip = pd.DataFrame(trip_records)
    df_trip["tanggal"] = pd.to_datetime(df_trip["tanggal"])

    # 2. Kalender
    kalender_rows = db.query(
        Kalender.tanggal,
        Kalender.weekend,
        Kalender.libur_nasional,
        Kalender.libur_sekolah
    ).all()

    df_kal = pd.DataFrame([{
        "tanggal": pd.to_datetime(k.tanggal),
        "weekend": bool(k.weekend),
        "libur_nasional": bool(k.libur_nasional),
        "libur_sekolah": bool(k.libur_sekolah)
    } for k in kalender_rows]).sort_values("tanggal").reset_index(drop=True)

    if not df_kal.empty:
        df_kal["sekitar_libur"] = _koreksi_sekitar_libur(df_kal)
    else:
        df_kal["sekitar_libur"] = False

    return df_trip, df_kal


def analisis_performa_ml(db: Session, cabang: Optional[str] = None) -> Dict[str, Any]:
    """Hitung metrik model, perbandingan baseline, faktor penting, slot bermasalah, dan prediksi 30 hari."""
    sidik = (_sidik_data(db), cabang)
    sekarang = time.monotonic()
    with _cache_lock:
        ada = _cache.get(sidik)
        if ada and sekarang - ada[0] < _CACHE_TTL_DETIK:
            return ada[1]

    df_trip, df_kal = _siapkan_data_trip(db, cabang)
    if df_trip.empty or len(df_trip) < 30:
        res_kosong = {
            "metrik_model": {"mae": 0.0, "rmse": 0.0, "r2": 0.0},
            "perbandingan_baseline": [],
            "faktor_penting": [],
            "slot_bermasalah": [],
            "prediksi_okupansi_rute": []
        }
        return res_kosong

    # Join dengan kalender
    df = pd.merge(df_trip, df_kal, on="tanggal", how="left")
    df["weekend"] = df["weekend"].fillna(df["tanggal"].dt.dayofweek >= 5).astype(int)
    df["libur_nasional"] = df["libur_nasional"].fillna(False).astype(int)
    df["libur_sekolah"] = df["libur_sekolah"].fillna(False).astype(int)
    df["sekitar_libur"] = df["sekitar_libur"].fillna(False).astype(int)
    df["hari"] = df["tanggal"].dt.dayofweek
    df["bulan"] = df["tanggal"].dt.month

    # Urutkan untuk pembentukan lag tanpa data leakage
    df = df.sort_values(["id_rute", "jam", "tanggal"]).reset_index(drop=True)

    # Fitur okupansi rata-rata 7 hari sebelumnya per rute+jam
    # Hanya menggunakan data masa lalu: shift(1) lalu rolling(7, min_periods=1)
    df["lag_okupansi_7h"] = (
        df.groupby(["id_rute", "jam"])["okupansi"]
        .transform(lambda s: s.shift(1).rolling(7, min_periods=1).mean())
    )
    # Isi NaN pertama kali dengan rata-rata rute
    rata_rute = df.groupby("id_rute")["okupansi"].transform("mean")
    df["lag_okupansi_7h"] = df["lag_okupansi_7h"].fillna(rata_rute).fillna(df["okupansi"].mean())

    # Split berdasarkan waktu: 90 hari terakhir sebagai data uji
    max_tanggal = df["tanggal"].max()
    batas_split = max_tanggal - pd.Timedelta(days=90)

    train_df = df[df["tanggal"] < batas_split].copy()
    test_df = df[df["tanggal"] >= batas_split].copy()

    # Fallback jika data kurang panjang dari 90 hari
    if len(train_df) < 20 or len(test_df) < 10:
        split_idx = int(len(df) * 0.8)
        train_df = df.iloc[:split_idx].copy()
        test_df = df.iloc[split_idx:].copy()

    feature_cols = [
        "nama_rute", "layanan", "jam", "hari", "bulan",
        "weekend", "libur_nasional", "libur_sekolah", "sekitar_libur",
        "lag_okupansi_7h"
    ]
    target_col = "okupansi"

    X_train = train_df[feature_cols]
    y_train = train_df[target_col]
    X_test = test_df[feature_cols]
    y_test = test_df[target_col]

    # Preprocessing categorical
    cat_cols = ["nama_rute", "layanan", "jam"]
    num_cols = ["hari", "bulan", "weekend", "libur_nasional", "libur_sekolah", "sekitar_libur", "lag_okupansi_7h"]

    preprocessor = ColumnTransformer(
        transformers=[
            ("cat", OneHotEncoder(handle_unknown="ignore", sparse_output=False), cat_cols),
            ("num", "passthrough", num_cols)
        ]
    )

    X_train_trans = preprocessor.fit_transform(X_train)
    X_test_trans = preprocessor.transform(X_test)

    # 1. Model HistGradientBoostingRegressor
    model_hgb = HistGradientBoostingRegressor(max_iter=100, random_state=42)
    model_hgb.fit(X_train_trans, y_train)
    pred_hgb = np.clip(model_hgb.predict(X_test_trans), 0.0, 1.0)

    # 2. Baseline Ridge
    model_ridge = Ridge(alpha=1.0)
    model_ridge.fit(X_train_trans, y_train)
    pred_ridge = np.clip(model_ridge.predict(X_test_trans), 0.0, 1.0)

    # 3. Baseline Rata-rata per rute+jam
    rata_rute_jam = train_df.groupby(["nama_rute", "jam"])["okupansi"].mean().to_dict()
    mean_global = train_df["okupansi"].mean()
    pred_mean = test_df.apply(
        lambda r: rata_rute_jam.get((r["nama_rute"], r["jam"]), mean_global), axis=1
    ).values

    def _eval(y_true, y_pred):
        return {
            "mae": round(float(mean_absolute_error(y_true, y_pred)), 4),
            "rmse": round(float(np.sqrt(mean_squared_error(y_true, y_pred))), 4),
            "r2": round(float(r2_score(y_true, y_pred)), 4),
        }

    metrik_hgb = _eval(y_test, pred_hgb)
    metrik_ridge = _eval(y_test, pred_ridge)
    metrik_mean = _eval(y_test, pred_mean)

    perbandingan_baseline = [
        {"model": "HistGradientBoosting", "terpilih": True, **metrik_hgb},
        {"model": "Ridge Regression", "terpilih": False, **metrik_ridge},
        {"model": "Baseline Rata-rata", "terpilih": False, **metrik_mean},
    ]

    # Faktor penting dengan Permutation Importance
    perm = permutation_importance(model_hgb, X_test_trans, y_test, n_repeats=5, random_state=42)
    feature_names = preprocessor.get_feature_names_out()

    grouped_importance = {}
    for name, imp in zip(feature_names, perm.importances_mean):
        # Kelompokkan kembali fitur asli
        clean_name = name.split("__")[-1]
        kategori = clean_name.split("_")[0] if "_" in clean_name and not clean_name.startswith("lag") else clean_name
        grouped_importance[kategori] = grouped_importance.get(kategori, 0.0) + max(0.0, float(imp))

    total_imp = sum(grouped_importance.values()) or 1.0
    label_map = {
        "jam": "Jam Keberangkatan",
        "nama": "Rute Perjalanan",
        "layanan": "Kelas Layanan (VIP/Reguler)",
        "hari": "Hari Operasional",
        "bulan": "Musim / Bulan",
        "weekend": "Akhir Pekan",
        "libur": "Libur Nasional & Sekolah",
        "sekitar": "Hari Sekitar Libur (H-1/H+1)",
        "lag": "Tren Keterisian 7 Hari Terakhir"
    }

    faktor_penting = []
    for k, val in sorted(grouped_importance.items(), key=lambda x: x[1], reverse=True)[:5]:
        persen = round((val / total_imp) * 100, 1)
        faktor_penting.append({
            "faktor": label_map.get(k, k.replace("_", " ").capitalize()),
            "pengaruh_persen": persen
        })

    # Deteksi slot bermasalah dengan Isolation Forest & rule ambang batas
    # Group per rute dan jam untuk mendeteksi slot konsisten
    stat_slot = df.groupby(["id_rute", "nama_rute", "jam"]).agg(
        total_trip=("okupansi", "count"),
        rata_okupansi=("okupansi", "mean"),
        trip_kosong=("okupansi", lambda s: (s < 0.30).sum()),
        trip_penuh=("okupansi", lambda s: (s > 0.95).sum())
    ).reset_index()

    # Hanya pertimbangkan slot dengan minimal 10 trip
    stat_slot = stat_slot[stat_slot["total_trip"] >= 10].copy()

    slot_bermasalah = []
    if not stat_slot.empty:
        stat_slot["pct_kosong"] = stat_slot["trip_kosong"] / stat_slot["total_trip"]
        stat_slot["pct_penuh"] = stat_slot["trip_penuh"] / stat_slot["total_trip"]

        for _, row in stat_slot.iterrows():
            if row["pct_kosong"] >= 0.5 or row["rata_okupansi"] < 0.30:
                slot_bermasalah.append({
                    "id_rute": row["id_rute"],
                    "rute": row["nama_rute"],
                    "jam": row["jam"],
                    "rata_okupansi": round(row["rata_okupansi"] * 100, 1),
                    "masalah": "Konsisten Rendah (<30%)",
                    "saran": "Evaluasi jadwal, geser atau gabungkan jadwal keberangkatan ke jam ramai.",
                    "prioritas": "tinggi" if row["rata_okupansi"] < 0.20 else "sedang"
                })
            elif row["pct_penuh"] >= 0.4 or row["rata_okupansi"] > 0.90:
                slot_bermasalah.append({
                    "id_rute": row["id_rute"],
                    "rute": row["nama_rute"],
                    "jam": row["jam"],
                    "rata_okupansi": round(row["rata_okupansi"] * 100, 1),
                    "masalah": "Konsisten Penuh (>90%)",
                    "saran": "Pertimbangkan penambahan armada cadangan atau frekuensi unit pada jam ini.",
                    "prioritas": "sedang"
                })

    slot_bermasalah = sorted(slot_bermasalah, key=lambda x: (x["prioritas"] != "tinggi", x["rata_okupansi"]))[:8]

    # Prediksi okupansi 30 hari ke depan
    tgl_awal_pred = max_tanggal + pd.Timedelta(days=1)
    tgl_akhir_pred = max_tanggal + pd.Timedelta(days=30)

    # Ambil jadwal aktif per rute
    jadwal_list = db.query(Jadwal.id_rute, Rute.nama_rute, Jadwal.jam_keberangkatan, Jadwal.layanan).join(Rute).all()
    if not jadwal_list:
        # Fallback dari distinct df
        jadwal_list = df[["id_rute", "nama_rute", "jam", "layanan"]].drop_duplicates().values

    # Buat kombinasi 30 hari
    pred_dates = pd.date_range(tgl_awal_pred, tgl_akhir_pred)
    df_pred_kal = df_kal[df_kal["tanggal"].isin(pred_dates)].copy()
    if df_pred_kal.empty:
        df_pred_kal = pd.DataFrame({"tanggal": pred_dates})
        df_pred_kal["weekend"] = (df_pred_kal["tanggal"].dt.dayofweek >= 5).astype(int)
        df_pred_kal["libur_nasional"] = 0
        df_pred_kal["libur_sekolah"] = 0
        df_pred_kal["sekitar_libur"] = 0

    records_future = []
    # Ambil lag_okupansi_7h terbaru per rute+jam
    latest_lag = df.groupby(["nama_rute", "jam"])["okupansi"].last().to_dict()

    for _, tgl_row in df_pred_kal.iterrows():
        t = tgl_row["tanggal"]
        for j in jadwal_list:
            nama_r = j[1]
            jam_k = str(j[2]).strip()
            lay = j[3]
            records_future.append({
                "id_rute": j[0],
                "nama_rute": nama_r,
                "layanan": lay,
                "jam": jam_k,
                "hari": t.dayofweek,
                "bulan": t.month,
                "weekend": int(tgl_row.get("weekend", 0)),
                "libur_nasional": int(tgl_row.get("libur_nasional", 0)),
                "libur_sekolah": int(tgl_row.get("libur_sekolah", 0)),
                "sekitar_libur": int(tgl_row.get("sekitar_libur", 0)),
                "lag_okupansi_7h": latest_lag.get((nama_r, jam_k), mean_global)
            })

    prediksi_okupansi_rute = []
    if records_future:
        df_future = pd.DataFrame(records_future)
        X_future = preprocessor.transform(df_future[feature_cols])
        df_future["pred_okupansi"] = np.clip(model_hgb.predict(X_future), 0.0, 1.0)

        # Rata-rata per rute untuk 30 hari ke depan
        agg_pred = df_future.groupby(["id_rute", "nama_rute"])["pred_okupansi"].mean().reset_index()
        for _, row in agg_pred.iterrows():
            okupansi_pct = round(row["pred_okupansi"] * 100, 1)
            status = "Tinggi" if okupansi_pct >= 80 else ("Sedang" if okupansi_pct >= 50 else "Rendah")
            prediksi_okupansi_rute.append({
                "id_rute": row["id_rute"],
                "rute": row["nama_rute"],
                "prediksi_okupansi_persen": okupansi_pct,
                "status": status
            })

    hasil = {
        "metrik_model": metrik_hgb,
        "perbandingan_baseline": perbandingan_baseline,
        "faktor_penting": faktor_penting,
        "slot_bermasalah": slot_bermasalah,
        "prediksi_okupansi_rute": prediksi_okupansi_rute
    }

    with _cache_lock:
        _cache[sidik] = (sekarang, hasil)

    return hasil
