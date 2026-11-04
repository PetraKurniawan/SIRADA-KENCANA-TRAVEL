"""
Service Reduksi Atribut (feature reduction) - tahap "Data Preprocessing" pada flowchart.

Pemilihan atribut tidak lagi berdasarkan penilaian manual, tetapi lewat 4 algoritma berurutan
yang bisa diaudit dan dijalankan ulang pada data apa pun:

  1. Filter kualitas (Variance Threshold + Missing Value Filter)
       - numerik : varians setelah Min-Max < VARIANS_MIN                  -> dibuang
       - kategorik: hanya 1 nilai, atau satu nilai dominan >= DOMINAN_MAKS -> dibuang
       - semua   : persen kosong >= KOSONG_MAKS                           -> dibuang
  2. Filter redundansi (korelasi antar-atribut)
       - numerik  x numerik  : |Pearson r| >= KORELASI_MAKS
       - kategorik x kategorik: Cramer's V >= KORELASI_MAKS
       - numerik terhadap target: |r| >= KORELASI_MAKS dianggap atribut turunan target
         (mis. total_harga = harga x jumlah, total_bayar = total_harga - diskon)
     Dari tiap pasangan redundan, atribut dengan Mutual Information (MI) lebih kecil dibuang.
  3. Filter relevansi (Mutual Information terhadap target)
       - target = total_bayar (nilai ekonomi tiap transaksi)
       - MI dinormalisasi terhadap MI terbesar; MI_norm < MI_NORM_MIN -> dibuang
  4. PCA (Principal Component Analysis) pada atribut numerik terstandarisasi sebagai bukti
     reduksi dimensi: berapa komponen yang cukup menjelaskan >= PCA_VARIANS_TARGET varians.

Atribut identifier (id_transaksi, id_pelanggan) dan kunci waktu (tanggal) tidak diseleksi
karena fungsinya sebagai kunci, bukan fitur. Atribut yang dibuang algoritma tetapi masih
dibutuhkan modul (mis. jam keberangkatan untuk menghitung trip) tetap ditandai
"Dipertahankan (dibutuhkan modul)" supaya sistem tidak rusak.
"""
import copy
import threading

import numpy as np
import pandas as pd
from scipy.stats import chi2_contingency
from sklearn.decomposition import PCA
from sklearn.feature_selection import mutual_info_regression
from sklearn.preprocessing import MinMaxScaler, StandardScaler
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.models import Transaksi

# --- Parameter algoritma (ditampilkan ke pengguna di bagian "metode") ---
VARIANS_MIN = 0.01          # varians minimum atribut numerik (skala Min-Max 0..1)
DOMINAN_MAKS = 0.99         # satu nilai >= 99% baris = quasi-konstan
KOSONG_MAKS = 0.50          # atribut kosong >= 50% dibuang
KORELASI_MAKS = 0.95        # ambang redundansi (Pearson / Cramer's V)
MI_NORM_MIN = 0.05          # MI relatif terhadap MI terbesar
PCA_VARIANS_TARGET = 0.90   # target varians kumulatif PCA
UKURAN_SAMPEL = 40000       # baris acak yang dianalisis (seluruh baris bila lebih sedikit)
UKURAN_SAMPEL_MI = 15000    # MI memakai kNN sehingga disampel lebih kecil lagi
RANDOM_STATE = 42
TARGET = "total_bayar"

KOLOM_NUMERIK = ["jumlah_unit", "harga_satuan", "diskon_per_tiket", "total_harga",
                 "total_diskon", "total_bayar"]
KOLOM_KATEGORIK = ["hari", "jam_keberangkatan", "id_rute", "id_armada", "cabang_asal",
                   "cabang_tujuan", "layanan", "jenis_transaksi", "id_member", "id_jenis_paket",
                   "satuan", "channel_pemesanan", "keterangan"]
KOLOM_KUNCI = {"id_transaksi": "Identifier transaksi", "id_pelanggan": "Identifier pelanggan (ID RFM)",
               "tanggal": "Kunci waktu (deret waktu)"}
 
# Modul sistem yang memakai tiap atribut (dipakai untuk menandai atribut yang tak boleh hilang).
DIPAKAI_MODUL = {
    "id_transaksi": ["Semua modul (kunci)"],
    "tanggal": ["EDA", "Forecasting", "Performa", "Segmentasi (Recency)"],
    "id_pelanggan": ["Segmentasi (ID pelanggan RFM)"],
    "id_rute": ["EDA", "Forecasting", "Performa", "Analisis Terpadu"],
    "jumlah_unit": ["Forecasting (target)", "Performa (okupansi)"],
    "total_bayar": ["Segmentasi (Monetary)", "Performa (pendapatan)"],
    "harga_satuan": ["Performa (rata-rata harga tiket)"],
    "id_armada": ["Performa (kapasitas trip)"],
    "jam_keberangkatan": ["Performa (penentu trip)"],
    "layanan": ["Performa (VIP vs Reguler)"],
    "jenis_transaksi": ["Semua modul (filter Penumpang)"],
    "cabang_asal": ["Hak akses Kepala Outlet", "Performa per cabang"],
    "id_member": ["Segmentasi (ringkasan member)"],
    "channel_pemesanan": ["EDA"],
}

METODE = [
    {"tahap": 1, "nama": "Filter kualitas",
     "algoritma": "Variance Threshold + Missing Value Filter",
     "parameter": f"varians (Min-Max) < {VARIANS_MIN}; nilai dominan >= {int(DOMINAN_MAKS * 100)}%; "
                  f"kosong >= {int(KOSONG_MAKS * 100)}%"},
    {"tahap": 2, "nama": "Filter redundansi",
     "algoritma": "Korelasi Pearson (numerik) dan Cramer's V (kategorik)",
     "parameter": f"|r| atau V >= {KORELASI_MAKS}; dari tiap pasangan, atribut ber-MI lebih kecil dibuang"},
    {"tahap": 3, "nama": "Filter relevansi",
     "algoritma": "Mutual Information terhadap target total_bayar",
     "parameter": f"MI dinormalisasi terhadap MI terbesar; dibuang bila < {MI_NORM_MIN}"},
    {"tahap": 4, "nama": "Reduksi dimensi (bukti)",
     "algoritma": "PCA pada atribut numerik terstandarisasi",
     "parameter": f"jumlah komponen minimum untuk varians kumulatif >= {int(PCA_VARIANS_TARGET * 100)}%"},
]

_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAKS = 16


# --------------------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------------------
def _muat_sampel(db: Session, cabang: str | None) -> tuple[pd.DataFrame, int]:
    kolom_db = ["id_transaksi", "tanggal", "id_pelanggan", *KOLOM_NUMERIK, *KOLOM_KATEGORIK]
    atribut = [getattr(Transaksi, k) for k in kolom_db]
    q_total = db.query(func.count(Transaksi.id_transaksi))
    q = db.query(*atribut)
    if cabang:
        q_total = q_total.filter(Transaksi.cabang_asal == cabang)
        q = q.filter(Transaksi.cabang_asal == cabang)
    total = int(q_total.scalar() or 0)
    if total > UKURAN_SAMPEL:
        q = q.order_by(func.random()).limit(UKURAN_SAMPEL)
    df = pd.DataFrame(q.all(), columns=kolom_db)
    return df, total


# --------------------------------------------------------------------------------------
# Algoritma
# --------------------------------------------------------------------------------------
def _cramers_v(a: pd.Series, b: pd.Series) -> float:
    tabel = pd.crosstab(a.fillna("(kosong)"), b.fillna("(kosong)"))
    if tabel.shape[0] < 2 or tabel.shape[1] < 2:
        return 0.0
    chi2 = chi2_contingency(tabel, correction=False)[0]
    n = tabel.to_numpy().sum()
    return float(np.sqrt(chi2 / (n * (min(tabel.shape) - 1)))) if n else 0.0


def _mutual_information(df: pd.DataFrame, fitur: list[str]) -> dict[str, float]:
    """MI(atribut ; total_bayar). Kategorik di-encode ordinal & ditandai diskret."""
    sampel = df if len(df) <= UKURAN_SAMPEL_MI else df.sample(UKURAN_SAMPEL_MI, random_state=RANDOM_STATE)
    kolom, diskret = [], []
    for f in fitur:
        if f in KOLOM_NUMERIK:
            kolom.append(sampel[f].fillna(0).astype(float).to_numpy())
            diskret.append(False)
        else:
            kolom.append(pd.factorize(sampel[f].fillna("(kosong)"))[0].astype(float))
            diskret.append(True)
    if not kolom:
        return {}
    X = np.column_stack(kolom)
    y = sampel[TARGET].astype(float).to_numpy()
    mi = mutual_info_regression(X, y, discrete_features=np.array(diskret), random_state=RANDOM_STATE)
    return {f: float(v) for f, v in zip(fitur, mi)}


def _pca(df: pd.DataFrame, fitur: list[str]) -> dict:
    if len(fitur) < 2 or len(df) < 10:
        return {"fitur": fitur, "komponen": [], "jumlah_komponen_target": None, "loading": []}
    X = StandardScaler().fit_transform(df[fitur].fillna(0).astype(float).to_numpy())
    pca = PCA(random_state=RANDOM_STATE).fit(X)
    rasio = pca.explained_variance_ratio_
    kum = np.cumsum(rasio)
    jumlah = int(np.searchsorted(kum, PCA_VARIANS_TARGET - 1e-9) + 1)
    komponen = [{"pc": i + 1, "varians_persen": round(float(r) * 100, 2),
                 "kumulatif_persen": round(float(k) * 100, 2)} for i, (r, k) in enumerate(zip(rasio, kum))]
    n_load = min(3, len(fitur))
    loading = [{"fitur": f, **{f"pc{j + 1}": round(float(pca.components_[j][i]), 3)
                               for j in range(n_load)}} for i, f in enumerate(fitur)]
    return {"fitur": fitur, "komponen": komponen,
            "jumlah_komponen_target": jumlah, "varians_target_persen": int(PCA_VARIANS_TARGET * 100),
            "loading": loading}


def _jalankan(db: Session, cabang: str | None) -> dict:
    df, total = _muat_sampel(db, cabang)
    n = len(df)
    kosong_hasil = {"metode": METODE, "ringkasan": {"baris_total": total, "baris_dianalisis": n,
                    "disampel": False, "atribut_awal": 0, "atribut_dipertahankan": 0,
                    "atribut_dibuang": 0, "target": TARGET}, "atribut": [],
                    "pasangan_redundan": [], "pca": {"fitur": [], "komponen": [], "loading": []}}
    if n < 100:
        kosong_hasil["pesan"] = "Data terlalu sedikit untuk reduksi atribut (minimal 100 baris)."
        return kosong_hasil

    fitur_semua = KOLOM_NUMERIK + KOLOM_KATEGORIK
    info: dict[str, dict] = {}
    for f in fitur_semua:
        s = df[f]
        persen_kosong = float(s.isna().mean())
        isi = s.dropna()
        item = {"kolom": f, "tipe": "numerik" if f in KOLOM_NUMERIK else "kategorik",
                "nilai_unik": int(isi.nunique()), "persen_kosong": round(100 * persen_kosong, 2),
                "dominan_persen": None, "varians": None, "mi": None, "mi_norm": None,
                "korelasi_maks": None, "pasangan_korelasi": None,
                "tahap_buang": None, "alasan": None}
        if len(isi):
            item["dominan_persen"] = round(float(100 * isi.value_counts(normalize=True).iloc[0]), 2)
        if f in KOLOM_NUMERIK and len(isi):
            skala = MinMaxScaler().fit_transform(isi.astype(float).to_numpy().reshape(-1, 1)).ravel()
            item["varians"] = round(float(skala.var()), 5)
        info[f] = item

    # ---- Tahap 1: filter kualitas ----
    for f, it in info.items():
        if it["persen_kosong"] / 100 >= KOSONG_MAKS:
            it["tahap_buang"], it["alasan"] = 1, f"{it['persen_kosong']:.0f}% nilai kosong (>= {int(KOSONG_MAKS * 100)}%)"
        elif it["nilai_unik"] <= 1:
            it["tahap_buang"], it["alasan"] = 1, "hanya satu nilai (konstan)"
        elif it["tipe"] == "kategorik" and it["dominan_persen"] / 100 >= DOMINAN_MAKS:
            it["tahap_buang"], it["alasan"] = 1, f"satu nilai mendominasi {it['dominan_persen']:.1f}% baris (quasi-konstan)"
        elif it["tipe"] == "numerik" and it["varians"] is not None and it["varians"] < VARIANS_MIN:
            it["tahap_buang"], it["alasan"] = 1, f"varians {it['varians']:.4f} < {VARIANS_MIN}"

    lolos1 = [f for f in fitur_semua if info[f]["tahap_buang"] is None]

    # MI dihitung untuk atribut yang lolos tahap 1 (dipakai tahap 2 untuk memilih mana yang dibuang
    # dan tahap 3 untuk relevansi). Target sendiri tidak dinilai sebagai fitur.
    kandidat_mi = [f for f in lolos1 if f != TARGET]
    mi = _mutual_information(df, kandidat_mi)
    mi_maks = max(mi.values(), default=0.0)
    for f, v in mi.items():
        info[f]["mi"] = round(v, 4)
        info[f]["mi_norm"] = round(v / mi_maks, 4) if mi_maks > 0 else 0.0

    # ---- Tahap 2: redundansi ----
    pasangan: list[dict] = []
    num = [f for f in lolos1 if f in KOLOM_NUMERIK]
    kat = [f for f in lolos1 if f in KOLOM_KATEGORIK]

    if TARGET in num:
        for f in num:
            if f == TARGET:
                continue
            r = float(df[f].astype(float).corr(df[TARGET].astype(float)))
            if np.isnan(r):
                continue
            _catat_korelasi(info, f, TARGET, abs(r))
            if abs(r) >= KORELASI_MAKS and info[f]["tahap_buang"] is None:
                info[f]["tahap_buang"] = 2
                info[f]["alasan"] = f"|r| = {abs(r):.3f} dengan target {TARGET} (atribut turunan target)"
                pasangan.append({"a": f, "b": TARGET, "ukuran": "Pearson", "nilai": round(r, 3),
                                 "dibuang": f})

    num_fitur = [f for f in num if f != TARGET and info[f]["tahap_buang"] is None]
    if len(num_fitur) > 1:
        korelasi = df[num_fitur].astype(float).corr()
        for i, a in enumerate(num_fitur):
            for b in num_fitur[i + 1:]:
                r = float(korelasi.loc[a, b])
                if np.isnan(r):
                    continue
                _catat_korelasi(info, a, b, abs(r))
                _catat_korelasi(info, b, a, abs(r))
                if abs(r) >= KORELASI_MAKS and info[a]["tahap_buang"] is None and info[b]["tahap_buang"] is None:
                    _buang_pasangan(info, a, b, "Pearson", r, pasangan)

    kat_aktif = list(kat)
    for i, a in enumerate(kat_aktif):
        for b in kat_aktif[i + 1:]:
            if info[a]["tahap_buang"] is not None or info[b]["tahap_buang"] is not None:
                continue
            v = _cramers_v(df[a], df[b])
            _catat_korelasi(info, a, b, v)
            _catat_korelasi(info, b, a, v)
            if v >= KORELASI_MAKS:
                _buang_pasangan(info, a, b, "Cramer's V", v, pasangan)

    # ---- Tahap 3: relevansi (MI) ----
    for f in fitur_semua:
        it = info[f]
        if it["tahap_buang"] is None and f != TARGET and it["mi_norm"] is not None \
                and it["mi_norm"] < MI_NORM_MIN:
            it["tahap_buang"] = 3
            it["alasan"] = f"MI relatif {it['mi_norm']:.3f} < {MI_NORM_MIN} (kontribusi informasi rendah terhadap {TARGET})"

    # ---- Status akhir ----
    atribut = []
    for f in fitur_semua:
        it = info[f]
        modul = DIPAKAI_MODUL.get(f, [])
        if f == TARGET:
            status, alasan = "Dipertahankan", "Target analisis (variabel yang diprediksi/dijelaskan)"
        elif it["tahap_buang"] is None:
            status, alasan = "Dipertahankan", "Lolos semua tahap seleksi"
        elif modul:
            status = "Dipertahankan (dibutuhkan modul)"
            alasan = f"Algoritma menyarankan dibuang ({it['alasan']}), tetapi dipakai: {', '.join(modul)}"
        else:
            status, alasan = "Dibuang", it["alasan"]
        atribut.append({**it, "dipakai_modul": modul, "status": status, "keterangan": alasan,
                        "keputusan_algoritma": "Dipertahankan" if it["tahap_buang"] is None else f"Dibuang (tahap {it['tahap_buang']})"})
    for f, ket in KOLOM_KUNCI.items():
        atribut.append({"kolom": f, "tipe": "kunci", "nilai_unik": int(df[f].nunique()) if f in df else 0,
                        "persen_kosong": round(float(100 * df[f].isna().mean()), 2) if f in df else 0.0,
                        "dominan_persen": None, "varians": None, "mi": None, "mi_norm": None,
                        "korelasi_maks": None, "pasangan_korelasi": None, "tahap_buang": None,
                        "alasan": None, "dipakai_modul": DIPAKAI_MODUL.get(f, []),
                        "status": "Dipertahankan (kunci)", "keterangan": ket,
                        "keputusan_algoritma": "Tidak diseleksi"})

    # PCA atas atribut numerik yang lolos tahap 1 (sebelum redundansi) - bukti bahwa dimensinya
    # dapat diringkas.
    pca = _pca(df, num)
    dipertahankan = sum(1 for a in atribut if a["status"].startswith("Dipertahankan"))
    return {
        "metode": METODE,
        "ringkasan": {
            "baris_total": total, "baris_dianalisis": n, "disampel": total > n,
            "atribut_awal": len(atribut), "atribut_dipertahankan": dipertahankan,
            "atribut_dibuang": len(atribut) - dipertahankan, "target": TARGET,
            "atribut_diseleksi": len(fitur_semua),
            "dibuang_algoritma": sum(1 for a in atribut if a["keputusan_algoritma"].startswith("Dibuang")),
        },
        "atribut": atribut,
        "pasangan_redundan": pasangan,
        "pca": pca,
        "parameter": {"varians_min": VARIANS_MIN, "dominan_maks": DOMINAN_MAKS, "kosong_maks": KOSONG_MAKS,
                      "korelasi_maks": KORELASI_MAKS, "mi_norm_min": MI_NORM_MIN,
                      "pca_varians_target": PCA_VARIANS_TARGET},
    }


def _catat_korelasi(info: dict, a: str, b: str, nilai: float) -> None:
    it = info[a]
    if it["korelasi_maks"] is None or nilai > it["korelasi_maks"]:
        it["korelasi_maks"] = round(float(nilai), 3)
        it["pasangan_korelasi"] = b


def _buang_pasangan(info: dict, a: str, b: str, ukuran: str, nilai: float, pasangan: list) -> None:
    """Dari pasangan redundan, buang atribut ber-MI lebih kecil (seri: yang bernilai unik lebih sedikit)."""
    mi_a, mi_b = info[a]["mi"] or 0.0, info[b]["mi"] or 0.0
    if mi_a == mi_b:
        buang, simpan = (a, b) if info[a]["nilai_unik"] <= info[b]["nilai_unik"] else (b, a)
    else:
        buang, simpan = (a, b) if mi_a < mi_b else (b, a)
    info[buang]["tahap_buang"] = 2
    info[buang]["alasan"] = f"redundan dengan {simpan} ({ukuran} = {abs(nilai):.3f}); {simpan} lebih informatif (MI lebih besar)"
    pasangan.append({"a": a, "b": b, "ukuran": ukuran, "nilai": round(float(nilai), 3), "dibuang": buang})


def _signature(db: Session, cabang: str | None):
    q = db.query(func.count(Transaksi.id_transaksi), func.max(Transaksi.tanggal))
    if cabang:
        q = q.filter(Transaksi.cabang_asal == cabang)
    n, maks = q.one()
    return int(n or 0), str(maks) if maks else None


def reduksi_atribut(db: Session, cabang: str | None = None) -> dict:
    """Jalankan seluruh pipeline reduksi atribut. Hasil di-cache per (cabang, signature data)."""
    kunci = (cabang, _signature(db, cabang))
    with _CACHE_LOCK:
        hasil = _CACHE.get(kunci)
    if hasil is None:
        hasil = _jalankan(db, cabang)
        with _CACHE_LOCK:
            if len(_CACHE) >= _CACHE_MAKS:
                _CACHE.pop(next(iter(_CACHE)))
            _CACHE[kunci] = hasil
    return copy.deepcopy(hasil)
