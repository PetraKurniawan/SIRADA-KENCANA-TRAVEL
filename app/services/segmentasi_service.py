"""
Service Segmentasi Pasar: RFM (Recency, Frequency, Monetary) + Min-Max + K-Means.
Sesuai algoritma yang sudah disepakati di rancangan (P.3 pada DFD):

1. Hitung RFM per pelanggan (id_pelanggan = proxy ID pelanggan, transaksi Penumpang saja).
2. Transformasi log (log(1 + x)) untuk meredam perbedaan skala & pencilan, lalu normalisasi
   fitur R, F, M dengan Min-Max (rentang 0..1). Nilai Silhouette hasilnya bergantung pada dataset yang
   dimuat; lihat evaluasi.per_k pada respons API (jangan mengandalkan angka tetap di komentar).
3. Jalankan K-Means untuk K = 2..8, evaluasi dengan Elbow (inertia),
   Silhouette Coefficient dan Davies-Bouldin Index (DBI).
4. Pilih K dengan Silhouette tertinggi (kecuali K ditentukan pemanggil),
   lalu beri label segmen dari centroid RFM tiap cluster.
"""
import copy
import threading
import warnings

import numpy as np
import pandas as pd
from sklearn import config_context
from sklearn.cluster import KMeans
from sklearn.metrics import davies_bouldin_score, silhouette_score
from sklearn.preprocessing import MinMaxScaler
from sqlalchemy import func
from threadpoolctl import threadpool_limits
from sqlalchemy.orm import Session

from app.models.models import Member, Rute, Transaksi

# Ambang kualitas cluster: Silhouette >= 0.5 dianggap struktur cluster yang wajar/kuat.
SILHOUETTE_MIN_VALID = 0.5
K_MIN, K_MAX = 2, 8
SILHOUETTE_SAMPLE = 10000
WORKING_MEMORY_MB = 32
RANDOM_STATE = 42

# Cache hasil di memori: kunci = (cabang, n_clusters, jumlah baris, tanggal terakhir).
_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAKS = 32


def _filter_penumpang(q, cabang: str | None):
    q = q.filter(Transaksi.jenis_transaksi == "Penumpang")
    if cabang:
        q = q.filter(Transaksi.cabang_asal == cabang)
    return q


def ringkasan_per_member(db: Session, cabang: str | None = None):
    """Sesuai 3 kartu di UI Segmentasi: total transaksi/pendapatan/rata-rata/rute favorit per jenis member.
    `cabang` (opsional) membatasi ke transaksi yang berangkat dari cabang tsb (scope Kepala Outlet)."""
    q = _filter_penumpang(
        db.query(Member.jenis_member,
                 func.count(Transaksi.id_transaksi).label("total_transaksi"),
                 func.sum(Transaksi.total_bayar).label("total_pendapatan"))
          .join(Transaksi, Transaksi.id_member == Member.id_member),
        cabang,
    ).group_by(Member.jenis_member).all()

    grand_total = sum(r.total_transaksi for r in q) or 1
    hasil = []
    for r in q:
        # rute favorit per jenis member
        rute_fav = _filter_penumpang(
            db.query(Rute.nama_rute, func.count(Transaksi.id_transaksi).label("n"))
              .join(Transaksi, Transaksi.id_rute == Rute.id_rute)
              .join(Member, Member.id_member == Transaksi.id_member)
              .filter(Member.jenis_member == r.jenis_member),
            cabang,
        ).group_by(Rute.nama_rute).order_by(func.count(Transaksi.id_transaksi).desc()).first()
        hasil.append({
            "jenis_member": r.jenis_member,
            "total_transaksi": r.total_transaksi,
            "total_pendapatan": float(r.total_pendapatan or 0),
            "rata_rata_transaksi": float((r.total_pendapatan or 0) / r.total_transaksi) if r.total_transaksi else 0.0,
            "rute_favorit": rute_fav.nama_rute if rute_fav else "-",
            "share_persen": round(100 * r.total_transaksi / grand_total, 1),
        })
    return sorted(hasil, key=lambda x: -x["total_transaksi"])


def _signature(db: Session, cabang: str | None):
    """Tanda tangan data: jumlah baris + tanggal terakhir. Berubah bila ada import/tambah data."""
    q = db.query(func.count(Transaksi.id_transaksi), func.max(Transaksi.tanggal))
    if cabang:
        q = q.filter(Transaksi.cabang_asal == cabang)
    n, maks = q.one()
    return int(n or 0), str(maks) if maks else None


def _hitung_rfm(db: Session, cabang: str | None) -> pd.DataFrame:
    """Agregasi RFM langsung di database (GROUP BY), hanya ringkasan per pelanggan yang ditarik."""
    rows = _filter_penumpang(
        db.query(Transaksi.id_pelanggan,
                 func.max(Transaksi.id_transaksi).label("kode_booking"),
                 func.max(Transaksi.tanggal).label("terakhir"),
                 func.count(Transaksi.id_transaksi).label("frequency"),
                 func.sum(Transaksi.total_bayar).label("monetary"))
          .filter(Transaksi.id_pelanggan.isnot(None), Transaksi.id_pelanggan != ""),
        cabang,
    ).group_by(Transaksi.id_pelanggan).all()
    df = pd.DataFrame(rows, columns=["nama_pelanggan", "kode_booking", "terakhir", "frequency", "monetary"])
    if df.empty:
        return df
    df["terakhir"] = pd.to_datetime(df["terakhir"])
    tanggal_acuan = df["terakhir"].max()  # tanggal terakhir pada dataset (scope yang sama)
    df["recency_hari"] = (tanggal_acuan - df["terakhir"]).dt.days.astype(int)
    df["frequency"] = df["frequency"].astype(int)
    df["monetary"] = df["monetary"].astype(float)
    return df


def _kmeans(X: np.ndarray, k: int) -> KMeans:
    # Data RFM kecil (satu baris per pelanggan): 1 thread lebih cepat & stabil di CPU bersama.
    with warnings.catch_warnings(), threadpool_limits(limits=1):
        warnings.simplefilter("ignore")  # peringatan memory-leak MKL di Windows
        return KMeans(n_clusters=k, random_state=RANDOM_STATE, n_init=10).fit(X)


def _evaluasi(X: np.ndarray, labels: np.ndarray) -> tuple[float | None, float | None]:
    if len(set(labels)) < 2:
        return None, None
    sample = SILHOUETTE_SAMPLE if len(X) > SILHOUETTE_SAMPLE else None
    # Silhouette menghitung jarak antar-semua-titik. Tanpa batas, 10.000 pelanggan butuh
    # ±1,5 GB RAM (melebihi batas container Railway). working_memory memaksa sklearn
    # menghitungnya per potongan kecil (MB) dengan hasil yang sama.
    with config_context(working_memory=WORKING_MEMORY_MB):
        sil = float(silhouette_score(X, labels, sample_size=sample, random_state=RANDOM_STATE))
    dbi = float(davies_bouldin_score(X, labels))
    return sil, dbi


def nama_segmen(k: int) -> list[str]:
    """Nama segmen berurutan dari skor komposit tertinggi ke terendah untuk K cluster."""
    tengah = ["Pelanggan Loyal", "Pelanggan Potensial", "Pelanggan Reguler",
              "Pelanggan Perlu Perhatian", "Pelanggan Berisiko", "Pelanggan Jarang Aktif"]
    if k <= 1:
        return ["Pelanggan Utama"]
    return ["Pelanggan Utama"] + tengah[: k - 2] + ["Pelanggan Pasif"]


def _beri_label(centroid_norm: np.ndarray) -> tuple[dict[int, str], dict[int, float]]:
    """Beri label tiap cluster dari centroid RFM-nya (skala log + Min-Max 0..1; log monoton,
    jadi urutan baik-buruk tetap sama dengan nilai RFM aslinya).

    Aturan deterministik:
        skor = (1 - R_norm) + F_norm + M_norm
    Recency kecil berarti baru saja bertransaksi (baik), sehingga dibalik (1 - R);
    Frequency dan Monetary besar berarti baik. Cluster diurutkan dari skor tertinggi
    ke terendah (seri dipecah dengan nomor cluster), lalu diberi nama berurutan:
    "Pelanggan Utama" (skor tertinggi), "Pelanggan Loyal", "Pelanggan Potensial",
    "Pelanggan Reguler", ... dan "Pelanggan Pasif" (skor terendah).
    Label bersifat relatif antar-cluster pada data yang sama, bukan ambang absolut.
    """
    skor = (1 - centroid_norm[:, 0]) + centroid_norm[:, 1] + centroid_norm[:, 2]
    urutan = sorted(range(len(skor)), key=lambda i: (-skor[i], i))
    nama = nama_segmen(len(skor))
    label_map = {cl: nama[i] for i, cl in enumerate(urutan)}
    skor_map = {cl: round(float(skor[cl]), 4) for cl in urutan}
    return label_map, skor_map


def _hasil_kosong(pesan: str) -> dict:
    return {"ringkasan_cluster": [], "pelanggan": [],
            "evaluasi": {"k_terpilih": None, "silhouette": None, "dbi": None, "valid": False,
                         "silhouette_min_valid": SILHOUETTE_MIN_VALID, "per_k": []},
            "jumlah_pelanggan": 0, "pesan": pesan}


def _jalankan(db: Session, n_clusters: int | None, cabang: str | None) -> dict:
    rfm = _hitung_rfm(db, cabang)
    n = len(rfm)
    if n < K_MIN + 1:
        return _hasil_kosong("Jumlah pelanggan terlalu sedikit untuk clustering.")

    fitur = ["recency_hari", "frequency", "monetary"]
    scaler = MinMaxScaler()
    X = scaler.fit_transform(np.log1p(rfm[fitur].to_numpy(dtype=float)))

    # Evaluasi K = 2..8 (K harus < jumlah pelanggan agar Silhouette terdefinisi).
    k_atas = min(K_MAX, n - 1)
    per_k, model_per_k = [], {}
    for k in range(K_MIN, k_atas + 1):
        km = _kmeans(X, k)
        sil, dbi = _evaluasi(X, km.labels_)
        model_per_k[k] = km
        per_k.append({"k": k, "inertia": round(float(km.inertia_), 6),
                      "silhouette": None if sil is None else round(sil, 4),
                      "dbi": None if dbi is None else round(dbi, 4)})

    kandidat = [p for p in per_k if p["silhouette"] is not None]
    if n_clusters is not None and n_clusters in model_per_k:
        k_terpilih, mode = n_clusters, "manual"
    elif kandidat:
        k_terpilih, mode = max(kandidat, key=lambda p: (p["silhouette"], -p["k"]))["k"], "otomatis"
    else:
        k_terpilih, mode = K_MIN, "otomatis"
    km = model_per_k[k_terpilih]
    eval_k = next(p for p in per_k if p["k"] == k_terpilih)

    rfm["cluster"] = km.labels_
    label_map, skor_map = _beri_label(km.cluster_centers_)
    rfm["label"] = rfm["cluster"].map(label_map)

    ringkasan = []
    for cl in sorted(label_map, key=lambda c: -skor_map[c]):
        sub = rfm[rfm["cluster"] == cl]
        ringkasan.append({
            "cluster": int(cl),
            "label": label_map[cl],
            "jumlah_pelanggan": int(len(sub)),
            "persen_pelanggan": round(100 * len(sub) / n, 1),
            "rata_recency_hari": round(float(sub["recency_hari"].mean()), 1),
            "rata_frequency": round(float(sub["frequency"].mean()), 1),
            "rata_monetary": round(float(sub["monetary"].mean()), 0),
            "skor": skor_map[cl],
        })

    pelanggan = (rfm.sort_values("monetary", ascending=False)
                    [["kode_booking", "nama_pelanggan", "frequency", "monetary", "recency_hari", "label", "cluster"]]
                    .rename(columns={"kode_booking": "kode_booking",
                                     "nama_pelanggan": "penumpang",
                                     "frequency": "transaksi",
                                     "monetary": "belanja",
                                     "recency_hari": "recency_hari",
                                     "label": "segmen",
                                     "cluster": "cluster"}))
    pelanggan["transaksi"] = pelanggan["transaksi"].astype(int)
    pelanggan["recency_hari"] = pelanggan["recency_hari"].astype(int)
    pelanggan["cluster"] = pelanggan["cluster"].astype(int)
    pelanggan["belanja"] = pelanggan["belanja"].astype(float)
    pelanggan["kode_booking"] = pelanggan["kode_booking"].astype(str)

    sil = eval_k["silhouette"]
    return {
        "ringkasan_cluster": ringkasan,
        "pelanggan": pelanggan.to_dict(orient="records"),  # dipotong sesuai limit oleh pemanggil
        "evaluasi": {
            "k_terpilih": k_terpilih,
            "mode": mode,
            "silhouette": sil,
            "dbi": eval_k["dbi"],
            "valid": bool(sil is not None and sil >= SILHOUETTE_MIN_VALID),
            "silhouette_min_valid": SILHOUETTE_MIN_VALID,
            "per_k": per_k,
        },
        "jumlah_pelanggan": n,
        "normalisasi": "log(1+x) lalu Min-Max (0-1)",
        "fitur": fitur,
    }


def jalankan_rfm_kmeans(db: Session, n_clusters: int | None = None, cabang: str | None = None,
                        limit_pelanggan: int = 5000) -> dict:
    """Hitung RFM per pelanggan (id_pelanggan sbg proxy ID), normalisasi Min-Max, lalu K-Means.

    - n_clusters None -> K dipilih otomatis (Silhouette tertinggi pada K = 2..8).
    - cabang -> hanya transaksi dari cabang_asal tsb.
    Mengembalikan dict: ringkasan_cluster, pelanggan (urut total belanja, maks limit), evaluasi.
    Hasil di-cache per (cabang, n_clusters, signature data)."""
    if n_clusters is not None and not (K_MIN <= n_clusters <= K_MAX):
        raise ValueError(f"n_clusters harus di antara {K_MIN} dan {K_MAX}")
    kunci = (cabang, n_clusters, _signature(db, cabang))
    with _CACHE_LOCK:
        hasil = _CACHE.get(kunci)
    if hasil is None:
        hasil = _jalankan(db, n_clusters, cabang)
        with _CACHE_LOCK:
            if len(_CACHE) >= _CACHE_MAKS:
                _CACHE.pop(next(iter(_CACHE)))
            _CACHE[kunci] = hasil
    # Salinan agar pemanggil tidak mengubah isi cache.
    keluaran = {k: copy.deepcopy(v) for k, v in hasil.items() if k != "pelanggan"}
    keluaran["pelanggan"] = [dict(p) for p in hasil["pelanggan"][: max(0, int(limit_pelanggan))]]
    return keluaran
