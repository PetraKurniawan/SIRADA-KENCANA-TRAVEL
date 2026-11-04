"""
Pembuatan laporan analisis (PDF via reportlab, Excel via openpyxl).

Laporan disusun dulu sebagai struktur netral (meta, KPI, daftar bagian bertabel), lalu
dirender ke PDF atau XLSX. Tiap bagian dibungkus try/except: bila satu analisis gagal (mis.
data belum cukup), bagian itu ditulis "data belum tersedia" dan laporan tetap jadi.
"""
import io
import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models.models import Rute, Transaksi

log = logging.getLogger(__name__)

JENIS_LAPORAN = ("ringkasan", "lengkap", "terpadu", "performa", "segmentasi", "forecasting")
FORMAT_LAPORAN = ("pdf", "xlsx")
JUDUL_JENIS = {
    "lengkap": "Laporan Lengkap Analisis",
    "terpadu": "Analisis Terpadu Segmentasi - Forecasting - Performa Rute",
    "ringkasan": "Ringkasan (Performa, Segmentasi, Forecasting)",
    "performa": "Performa Rute & Cabang",
    "segmentasi": "Segmentasi Pelanggan (RFM + K-Means)",
    "forecasting": "Forecasting Permintaan 30 Hari",
}
WIB = timezone(timedelta(hours=7))
PESAN_KOSONG = "Data belum tersedia"

# Tipe kolom untuk format angka: "teks" | "int" | "rp" | "persen" | "desimal"


def _bagian(judul, kolom, tipe, baris, catatan=None):
    return {"judul": judul, "kolom": kolom, "tipe": tipe, "baris": baris, "catatan": catatan,
            "error": None}


def _bagian_gagal(judul, e: Exception | str):
    pesan = str(e) if isinstance(e, str) else f"{PESAN_KOSONG} ({e.__class__.__name__}: {e})"
    log.warning("Bagian laporan '%s' gagal: %s", judul, pesan)
    return {"judul": judul, "kolom": [], "tipe": [], "baris": [], "catatan": None,
            "error": pesan[:300]}


# --------------------------------------------------------------------------- KPI
def kpi_transaksi(db: Session, mulai: date | None, selesai: date | None,
                  cabang: str | None) -> list[tuple[str, float, str]]:
    penumpang = Transaksi.jenis_transaksi == "Penumpang"
    q = select(
        func.count(Transaksi.id_transaksi),
        func.sum(case((penumpang, 1), else_=0)),
        func.sum(case((penumpang, Transaksi.jumlah_unit), else_=0)),
        func.sum(case((penumpang, 0), else_=Transaksi.jumlah_unit)),
        func.sum(Transaksi.total_bayar),
        func.sum(case((penumpang, Transaksi.total_bayar), else_=0)),
        func.sum(case((penumpang, 0), else_=Transaksi.total_bayar)),
        func.count(func.distinct(Transaksi.tanggal)),
    )
    if mulai:
        q = q.where(Transaksi.tanggal >= mulai)
    if selesai:
        q = q.where(Transaksi.tanggal <= selesai)
    if cabang:
        q = q.where(Transaksi.cabang_asal == cabang)
    n, n_pnp, pnp, kg, total, rp_pnp, rp_paket, hari = db.execute(q).one()
    hari = hari or 0
    total = float(total or 0)
    return [
        ("Total transaksi", n or 0, "int"),
        ("Transaksi penumpang", n_pnp or 0, "int"),
        ("Total penumpang (orang)", pnp or 0, "int"),
        ("Total berat paket (kg)", kg or 0, "int"),
        ("Total pendapatan", total, "rp"),
        ("Pendapatan penumpang", float(rp_pnp or 0), "rp"),
        ("Pendapatan paket", float(rp_paket or 0), "rp"),
        ("Hari beroperasi", hari, "int"),
        ("Rata-rata pendapatan per hari", total / hari if hari else 0, "rp"),
    ]


# --------------------------------------------------------------------------- bagian analisis
def _bagian_performa(db, mulai, selesai, cabang) -> list[dict]:
    from app.services import performa_service as ps
    hasil = []
    try:
        data = ps.analisis_performa_rute(db, cabang=cabang, tanggal_mulai=mulai,
                                         tanggal_selesai=selesai)
        if not data:
            raise ValueError("tidak ada transaksi penumpang pada periode ini")
        hasil.append(_bagian(
            "Performa per Rute",
            ["Rute", "Total Trip", "Penumpang", "Pendapatan", "Okupansi (%)"],
            ["teks", "int", "int", "rp", "persen"],
            [[d.get("rute"), d.get("total_trip"), d.get("total_penumpang"),
              d.get("total_pendapatan"), d.get("okupansi_persen")] for d in data],
            "Okupansi = rata-rata okupansi per perjalanan (penumpang / kapasitas armada x 100%)."))
    except Exception as e:  # noqa: BLE001
        hasil.append(_bagian_gagal("Performa per Rute", e))
    try:
        data = ps.analisis_performa_cabang(db, cabang=cabang, tanggal_mulai=mulai,
                                           tanggal_selesai=selesai)
        if not data:
            raise ValueError("tidak ada transaksi penumpang pada periode ini")
        hasil.append(_bagian(
            "Performa per Cabang",
            ["Cabang", "Total Trip", "Penumpang", "Pendapatan", "Okupansi (%)"],
            ["teks", "int", "int", "rp", "persen"],
            [[d.get("cabang"), d.get("total_trip"), d.get("total_penumpang"),
              d.get("total_pendapatan"), d.get("okupansi_persen")] for d in data]))
    except Exception as e:  # noqa: BLE001
        hasil.append(_bagian_gagal("Performa per Cabang", e))
    return hasil


def _bagian_segmentasi(db, cabang) -> list[dict]:
    from app.services import segmentasi_service as ss
    hasil = []
    try:
        data = ss.jalankan_rfm_kmeans(db, n_clusters=None, cabang=cabang)
        ringkasan = data.get("ringkasan_cluster") or []
        if not ringkasan:
            raise ValueError(data.get("pesan") or "belum ada pelanggan untuk disegmentasi")
        hasil.append(_bagian(
            "Ringkasan Cluster Pelanggan",
            ["Segmen", "Jumlah Pelanggan", "Rata Recency (hari)", "Rata Frequency",
             "Rata Monetary"],
            ["teks", "int", "desimal", "desimal", "rp"],
            [[c.get("label"), c.get("jumlah_pelanggan"), c.get("rata_recency_hari"),
              c.get("rata_frequency"), c.get("rata_monetary")] for c in ringkasan],
            "Segmentasi memakai seluruh riwayat transaksi penumpang (tidak dibatasi periode)."))
        ev = data.get("evaluasi") or {}
        valid = ev.get("valid")
        hasil.append(_bagian(
            "Evaluasi Clustering",
            ["Metrik", "Nilai"], ["teks", "teks"],
            [["Jumlah cluster (k) terpilih", _teks(ev.get("k_terpilih"))],
             ["Silhouette Score (makin mendekati 1 makin baik)", _teks(ev.get("silhouette"))],
             ["Davies-Bouldin Index (makin kecil makin baik)", _teks(ev.get("dbi"))],
             ["Status validasi", "-" if valid is None else ("Valid" if valid else "Belum valid")]]))
        per_k = ev.get("per_k") or []
        if per_k:
            hasil.append(_bagian(
                "Evaluasi Jumlah Cluster (K = 2..8)",
                ["K", "Inertia (Elbow)", "Silhouette", "Davies-Bouldin"],
                ["int", "desimal", "desimal", "desimal"],
                [[p["k"], p["inertia"], p["silhouette"], p["dbi"]] for p in per_k],
                f"K terpilih = {ev.get('k_terpilih')} (Silhouette tertinggi). "
                f"Cluster dianggap valid bila Silhouette >= {ev.get('silhouette_min_valid')}."))
    except Exception as e:  # noqa: BLE001
        hasil.append(_bagian_gagal("Segmentasi Pelanggan", e))
    return hasil


def _forecast_satu(fs, db, nama_rute):
    try:
        return fs.jalankan_forecast(db, nama_rute, horizon_hari=30, pakai_kalender=True)
    except TypeError as e:
        # Kompatibel dengan versi service yang belum punya parameter pakai_kalender.
        if "pakai_kalender" not in str(e):
            raise
        return fs.jalankan_forecast(db, nama_rute, horizon_hari=30)


def _bagian_forecasting(db, cabang, detail: bool = False) -> list[dict]:
    from app.services import forecasting_service as fs
    try:
        q = select(Rute.nama_rute).order_by(Rute.nama_rute)
        if cabang:
            q = q.where(Rute.cabang_asal == cabang)
        rute_list = list(db.scalars(q))
        if not rute_list:
            raise ValueError("tidak ada rute")
        baris, gagal, banding = [], [], []
        for nama in rute_list:
            try:
                f = _forecast_satu(fs, db, nama)
                for m in f.get("perbandingan") or []:
                    banding.append([nama, m["model"], m["mae"], m["rmse"], m["mape_persen"],
                                    m["kategori_mape"], "Ya" if m["terpilih"] else ""])
                valid = f.get("valid")
                baris.append([nama, f.get("model"), f.get("mape_persen"), f.get("mae"),
                              f.get("rmse"),
                              "-" if valid is None else ("Ya" if valid else "Tidak"),
                              f.get("prediksi_periode_berikutnya")])
            except Exception as e:  # noqa: BLE001
                gagal.append(nama)
                baris.append([nama, PESAN_KOSONG, None, None, None, "-", None])
                log.warning("Forecast rute %s gagal: %s", nama, e)
        catatan = ("Model dipilih otomatis per rute; prediksi = total penumpang 30 hari ke depan. "
                   "Forecasting memakai seluruh riwayat (tidak dibatasi periode).")
        if gagal:
            catatan += f" Rute tanpa hasil: {', '.join(gagal)}."
        hasil = [_bagian("Forecasting Permintaan per Rute",
                         ["Rute", "Model", "MAPE (%)", "MAE", "RMSE", "Valid",
                          "Prediksi 30 Hari"],
                         ["teks", "teks", "persen", "desimal", "desimal", "teks", "int"],
                         baris, catatan)]
        if detail and banding:
            hasil.append(_bagian(
                "Perbandingan Model Forecasting",
                ["Rute", "Model", "MAE", "RMSE", "MAPE (%)", "Kategori (Lewis)", "Terpilih"],
                ["teks", "teks", "desimal", "desimal", "persen", "teks", "teks"], banding,
                "Seluruh kandidat dievaluasi pada 28 hari terakhir (data uji); model dengan MAPE "
                "terkecil dipilih. Kategori MAPE menurut Lewis (1982)."))
        return hasil
    except Exception as e:  # noqa: BLE001
        return [_bagian_gagal("Forecasting Permintaan", e)]


def _bagian_metodologi() -> list[dict]:
    from app.services import integrasi_service as it
    from app.services import reduksi_atribut_service as ra
    baris = [
        ["Pembersihan data", "Validasi kolom wajib, buang baris kosong/tidak valid, hapus duplikat",
         "Import CSV (/api/import)"],
        ["Eksplorasi data (EDA)", "Statistik deskriptif, skewness/kurtosis, pencilan IQR, korelasi Pearson, "
         "ADF & ACF, pola hari-jam, efek kalender", "Tukey 1,5 x IQR; ADF alpha 0,05"],
    ] + [[f"Reduksi atribut tahap {m['tahap']}", f"{m['nama']}: {m['algoritma']}", m["parameter"]]
         for m in ra.METODE] + [
        ["Segmentasi pasar", "RFM -> log(1+x) -> Min-Max -> K-Means", "K = 2..8; dipilih Silhouette tertinggi; valid bila >= 0,5"],
        ["Forecasting demand", "SARIMAX (musiman + kalender, ordo AIC) vs ARIMA baseline", "Uji 28 hari terakhir; pilih MAPE terkecil; valid bila <= 20%"],
        ["Performa rute", "Statistik deskriptif + load factor", "Okupansi: Tinggi >= 80%, Sedang 50-80%, Rendah < 50%"],
    ] + [[a["tahap"], a["metode"], a["keluaran"]] for a in it.ALUR[3:]]
    return [_bagian("Metodologi Analisis", ["Tahap", "Metode / Algoritma", "Parameter / Keluaran"],
                    ["teks", "teks", "teks"], baris,
                    "Seluruh tahap dijalankan otomatis oleh sistem dan dapat diulang pada data baru.")]


def _bagian_eda(db, mulai, selesai, cabang) -> list[dict]:
    from app.services import eda_service as es
    try:
        d = es.analisis_eda(db, cabang=cabang, tanggal_mulai=mulai, tanggal_selesai=selesai)
        if not d["ringkasan"]["jumlah_baris"]:
            raise ValueError("tidak ada transaksi pada periode ini")
    except Exception as e:  # noqa: BLE001
        return [_bagian_gagal("Eksplorasi Data (EDA)", e)]
    r = d["ringkasan"]
    hasil = [_bagian("EDA - Ringkasan Dataset", ["Ukuran", "Nilai"], ["teks", "teks"], [
        ["Jumlah baris transaksi", format_nilai(r["jumlah_baris"], "int")],
        ["Rentang tanggal", f"{r['tanggal_awal']} s.d. {r['tanggal_akhir']} ({r['rentang_hari']} hari)"],
        ["Jumlah rute / cabang / pelanggan", f"{r['jumlah_rute']} / {r['jumlah_cabang']} / {format_nilai(r['jumlah_pelanggan'], 'int')}"],
        ["Total penumpang", format_nilai(r["total_penumpang"], "int")],
        ["Total pendapatan", format_nilai(r["total_pendapatan"], "rp")],
        ["Memenuhi syarat minimal baris analisis", "Ya" if r["memenuhi_min_baris"] else "Tidak"],
    ])]
    if d["kelengkapan"]:
        hasil.append(_bagian("EDA - Kelengkapan Data", ["Kolom", "Terisi", "Kosong", "% Kosong"],
                             ["teks", "int", "int", "persen"],
                             [[k["kolom"], k["terisi"], k["kosong"], k["persen_kosong"]] for k in d["kelengkapan"]]))
    if d["statistik"]:
        hasil.append(_bagian(
            "EDA - Statistik Deskriptif & Bentuk Distribusi",
            ["Variabel", "Mean", "Median", "Std", "Skewness", "Kurtosis", "% Outlier"],
            ["teks", "desimal", "desimal", "desimal", "desimal", "desimal", "persen"],
            [[x["variabel"], x["mean"], x["median"], x["std"], x["skewness"], x["kurtosis"],
              x["persen_outlier"]] for x in d["statistik"]],
            "Outlier memakai aturan Tukey (di luar Q1 - 1,5 IQR dan Q3 + 1,5 IQR). Skewness > 0,5 = miring kanan."))
    if d["efek_kalender"]:
        hasil.append(_bagian(
            "EDA - Efek Kalender terhadap Penumpang Harian",
            ["Faktor", "Rata-rata (ya)", "Rata-rata (tidak)", "Selisih (%)"],
            ["teks", "desimal", "desimal", "persen"],
            [[e["nama"], e["rata_penumpang_ya"], e["rata_penumpang_tidak"], e["selisih_persen"]]
             for e in d["efek_kalender"]],
            "Dasar pemilihan variabel eksogen (akhir pekan, libur nasional, libur sekolah) pada SARIMAX."))
    kor = d["korelasi"]
    if kor["variabel"]:
        hasil.append(_bagian("EDA - Matriks Korelasi Harian", ["Variabel"] + kor["label"],
                             ["teks"] + ["desimal"] * len(kor["label"]),
                             [[lab] + row for lab, row in zip(kor["label"], kor["matriks"])],
                             "Korelasi Pearson pada agregat harian."))
    o = d["outlier_harian"]
    dw = d["deret_waktu"]
    baris = [["Hari terdeteksi pencilan", f"{o['jumlah_outlier']} dari {o['jumlah_hari']} hari ({o['persen_outlier']}%)"]]
    if dw.get("adf_p_value") is not None:
        baris.append(["Uji ADF (stasioneritas)", f"p = {dw['adf_p_value']} -> {'stasioner' if dw['stasioner'] else 'tidak stasioner'}"])
    if dw.get("acf_lag7") is not None:
        baris.append(["Autokorelasi lag 7 (mingguan)", f"{dw['acf_lag7']} -> {'ada pola musiman mingguan' if dw['musiman_mingguan'] else 'tidak signifikan'}"])
    hasil.append(_bagian("EDA - Pencilan & Karakter Deret Waktu", ["Pengujian", "Hasil"], ["teks", "teks"], baris,
                         "Pola musiman mingguan menjadi alasan memakai model musiman (SARIMAX)."))
    return hasil


def _bagian_reduksi(db, cabang) -> list[dict]:
    from app.services import reduksi_atribut_service as ra
    try:
        d = ra.reduksi_atribut(db, cabang=cabang)
        if not d["atribut"]:
            raise ValueError(d.get("pesan") or "data belum cukup")
    except Exception as e:  # noqa: BLE001
        return [_bagian_gagal("Reduksi Atribut", e)]
    r = d["ringkasan"]
    hasil = [_bagian(
        "Reduksi Atribut - Hasil Seleksi",
        ["Atribut", "Tipe", "Nilai Unik", "% Kosong", "MI relatif", "Korelasi maks", "Keputusan algoritma", "Status akhir"],
        ["teks", "teks", "int", "persen", "desimal", "desimal", "teks", "teks"],
        [[a["kolom"], a["tipe"], a["nilai_unik"], a["persen_kosong"], a["mi_norm"], a["korelasi_maks"],
          a["keputusan_algoritma"], a["status"]] for a in d["atribut"]],
        f"{r['atribut_awal']} atribut awal -> {r['atribut_dipertahankan']} dipertahankan, {r['atribut_dibuang']} dibuang "
        f"(dianalisis {format_nilai(r['baris_dianalisis'], 'int')} dari {format_nilai(r['baris_total'], 'int')} baris). "
        f"Target MI = {r['target']}.")]
    if d["pasangan_redundan"]:
        hasil.append(_bagian("Reduksi Atribut - Pasangan Redundan", ["Atribut A", "Atribut B", "Ukuran", "Nilai", "Dibuang"],
                             ["teks", "teks", "teks", "desimal", "teks"],
                             [[x["a"], x["b"], x["ukuran"], x["nilai"], x["dibuang"]] for x in d["pasangan_redundan"]]))
    pca = d["pca"]
    if pca.get("komponen"):
        hasil.append(_bagian("Reduksi Atribut - PCA", ["Komponen", "Varians (%)", "Kumulatif (%)"],
                             ["teks", "persen", "persen"],
                             [[f"PC{k['pc']}", k["varians_persen"], k["kumulatif_persen"]] for k in pca["komponen"]],
                             f"{pca['jumlah_komponen_target']} komponen cukup untuk >= {pca['varians_target_persen']}% varians "
                             f"dari {len(pca['fitur'])} atribut numerik."))
    return hasil


def _bagian_terpadu(db, cabang) -> list[dict]:
    from app.services import integrasi_service as it
    try:
        d = it.analisis_terpadu(db, cabang=cabang)
    except Exception as e:  # noqa: BLE001
        return [_bagian_gagal("Analisis Terpadu", e)]
    hasil = [_bagian(
        "Terpadu - Ringkasan per Rute",
        ["Rute", "Okupansi (%)", "Status", "Segmen Inti (%)", "Prediksi 30 Hari", "Tumbuh (%)", "Aksi"],
        ["teks", "persen", "teks", "persen", "int", "persen", "teks"],
        [[r["rute"], r["performa"]["okupansi_persen"], r["performa"]["status"],
          r["segmentasi"]["persen_inti"] if r["segmentasi"] else None,
          r["forecasting"]["prediksi_30_hari"] if r["forecasting"] else None,
          r["forecasting"]["pertumbuhan_persen"] if r["forecasting"] else None,
          r["rekomendasi"].get("aksi", r["rekomendasi"]["prioritas"])] for r in d["rute"]],
        "Tumbuh = prediksi 30 hari ke depan dibanding 30 hari aktual terakhir. Laporan lengkap memuat: metodologi, EDA, reduksi atribut, segmentasi, forecasting, performa, dan analisis terpadu.")]
    hasil.append(_bagian(
        "Terpadu - Korelasi Antar-modul",
        ["Hubungan", "n", "Pearson r", "p", "Spearman rho", "Signifikansi"],
        ["teks", "int", "desimal", "desimal", "desimal", "teks"],
        [[f"{h['label_x']} - {h['label_y']}", h["hasil"]["n"] if h["hasil"] else None,
          h["hasil"]["pearson_r"] if h["hasil"] else None, h["hasil"]["pearson_p"] if h["hasil"] else None,
          h["hasil"]["spearman_rho"] if h["hasil"] else None,
          ("Signifikan" if h["hasil"]["signifikan"] else "Tidak signifikan") if h["hasil"] else "-"] for h in d["hubungan"]],
        d["kesimpulan"] + " Korelasi menunjukkan hubungan statistik, bukan sebab-akibat."))
    return hasil


def _bagian_tindak_lanjut(db: Session, cabang: str | None) -> list[dict]:
    from app.models.models import TindakLanjut, Rute
    q = db.query(TindakLanjut)
    if cabang:
        q = q.outerjoin(Rute, TindakLanjut.id_rute == Rute.id_rute).filter(Rute.cabang_asal == cabang)
    items = q.order_by(TindakLanjut.dibuat_pada.desc()).all()
    baris = []
    for item in items:
        rute_nama = item.rute.nama_rute if item.rute else "-"
        tgl_tenggat = item.tenggat.strftime("%d-%m-%Y") if item.tenggat else "-"
        baris.append([item.judul, rute_nama, item.prioritas.capitalize(), item.status.capitalize(), tgl_tenggat])
    return [_bagian(
        "Tindak Lanjut Temuan Analisis",
        ["Judul Tindakan", "Rute", "Prioritas", "Status", "Tenggat"],
        ["teks", "teks", "teks", "teks", "teks"],
        baris,
        "Daftar tindak lanjut operasional dan strategis yang dicatat berdasarkan temuan analisis."
    )]


def susun_laporan(db: Session, jenis: str, mulai: date | None, selesai: date | None,
                  cabang: str | None, dibuat_oleh: str) -> dict:
    if jenis not in JENIS_LAPORAN:
        raise ValueError(f"Jenis laporan tidak dikenal: {jenis}")
    bagian = []
    if jenis == "lengkap":
        bagian += _bagian_metodologi()
        bagian += _bagian_eda(db, mulai, selesai, cabang)
        bagian += _bagian_reduksi(db, cabang)
        bagian += _bagian_tindak_lanjut(db, cabang)
    if jenis in ("ringkasan", "lengkap", "performa"):
        bagian += _bagian_performa(db, mulai, selesai, cabang)
    if jenis in ("ringkasan", "lengkap", "segmentasi"):
        bagian += _bagian_segmentasi(db, cabang)
    if jenis in ("ringkasan", "lengkap", "forecasting"):
        bagian += _bagian_forecasting(db, cabang, detail=jenis in ("lengkap", "forecasting"))
    if jenis in ("lengkap", "terpadu"):
        bagian += _bagian_terpadu(db, cabang)

    try:
        kpi = kpi_transaksi(db, mulai, selesai, cabang)
    except Exception as e:  # noqa: BLE001
        log.warning("KPI laporan gagal: %s", e)
        kpi = []

    if mulai or selesai:
        periode = f"{_tgl(mulai) if mulai else 'awal data'} s.d. {_tgl(selesai) if selesai else 'akhir data'}"
    else:
        periode = "Seluruh data"
    return {
        "judul": "Laporan Analisis SIRADA Kencana",
        "subjudul": JUDUL_JENIS[jenis],
        "jenis": jenis,
        "meta": [
            ("Jenis laporan", JUDUL_JENIS[jenis]),
            ("Periode", periode),
            ("Cabang", cabang or "Semua cabang"),
            ("Dibuat pada", datetime.now(WIB).strftime("%d-%m-%Y %H:%M WIB")),
            ("Dibuat oleh", dibuat_oleh),
        ],
        "kpi": kpi,
        "bagian": bagian,
    }


# --------------------------------------------------------------------------- format angka
_BULAN = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]


def _tgl(d: date) -> str:
    return f"{d.day} {_BULAN[d.month - 1]} {d.year}"


def _teks(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.4f}".rstrip("0").rstrip(".")
    return str(v)


def _ribuan(n: float, desimal: int = 0) -> str:
    s = f"{n:,.{desimal}f}"
    return s.replace(",", "_").replace(".", ",").replace("_", ".")


def format_nilai(v, tipe: str) -> str:
    if v is None or v == "":
        return "-"
    if tipe == "teks":
        return str(v)
    try:
        n = float(v)
    except (TypeError, ValueError):
        return str(v)
    if tipe == "int":
        return _ribuan(round(n))
    if tipe == "rp":
        return "Rp " + _ribuan(round(n))
    if tipe == "persen":
        return _ribuan(n, 1) + "%"
    return _ribuan(n, 2)


# --------------------------------------------------------------------------- PDF
def render_pdf(lap: dict) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import (KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table,
                                    TableStyle)
    from xml.sax.saxutils import escape

    biru = colors.HexColor("#1e3a8a")
    abu = colors.HexColor("#f1f5f9")
    garis = colors.HexColor("#cbd5e1")
    ss = getSampleStyleSheet()
    s_judul = ParagraphStyle("judul", parent=ss["Title"], textColor=biru, fontSize=18,
                             spaceAfter=2, alignment=0)
    s_sub = ParagraphStyle("sub", parent=ss["Heading2"], textColor=colors.HexColor("#334155"),
                           fontSize=12, spaceBefore=0, spaceAfter=8)
    s_h = ParagraphStyle("h", parent=ss["Heading3"], textColor=biru, fontSize=11.5,
                         spaceBefore=12, spaceAfter=4)
    s_sel = ParagraphStyle("sel", parent=ss["BodyText"], fontSize=8.5, leading=10.5)
    s_sel_r = ParagraphStyle("selr", parent=s_sel, alignment=TA_RIGHT)
    s_head = ParagraphStyle("head", parent=s_sel, textColor=colors.white,
                            fontName="Helvetica-Bold")
    s_cat = ParagraphStyle("cat", parent=ss["BodyText"], fontSize=8,
                           textColor=colors.HexColor("#64748b"), spaceBefore=3)
    s_err = ParagraphStyle("err", parent=ss["BodyText"], fontSize=9,
                           textColor=colors.HexColor("#b45309"))

    lebar = A4[0] - 3.6 * cm
    buf = io.BytesIO()

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor("#64748b"))
        canvas.drawString(1.8 * cm, 1.1 * cm, f"{lap['judul']} - {lap['subjudul']}")
        canvas.drawRightString(A4[0] - 1.8 * cm, 1.1 * cm, f"Halaman {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=1.8 * cm, rightMargin=1.8 * cm,
                            topMargin=1.6 * cm, bottomMargin=1.8 * cm,
                            title=lap["judul"], author="SIRADA Kencana")
    el = [Paragraph(escape(lap["judul"]), s_judul), Paragraph(escape(lap["subjudul"]), s_sub)]

    meta = Table([[Paragraph(f"<b>{escape(k)}</b>", s_sel), Paragraph(escape(str(v)), s_sel)]
                  for k, v in lap["meta"]], colWidths=[4 * cm, lebar - 4 * cm])
    meta.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), abu),
                              ("BOX", (0, 0), (-1, -1), 0.5, garis),
                              ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                              ("TOPPADDING", (0, 0), (-1, -1), 3),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    el += [meta, Spacer(1, 6)]

    if lap["kpi"]:
        el.append(Paragraph("Indikator Utama Transaksi", s_h))
        sel = [[Paragraph(f"<font size=7.5 color='#64748b'>{escape(k)}</font><br/>"
                          f"<b>{escape(format_nilai(v, t))}</b>", s_sel)
                for k, v, t in lap["kpi"][i:i + 3]] for i in range(0, len(lap["kpi"]), 3)]
        for r in sel:
            r += [""] * (3 - len(r))
        t = Table(sel, colWidths=[lebar / 3] * 3)
        t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, garis),
                               ("INNERGRID", (0, 0), (-1, -1), 0.5, garis),
                               ("TOPPADDING", (0, 0), (-1, -1), 5),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
        el.append(t)

    for b in lap["bagian"]:
        judul = Paragraph(escape(b["judul"]), s_h)
        if b["error"]:
            el.append(KeepTogether([judul, Paragraph(escape(b["error"]), s_err)]))
            continue
        # Bobot lebar kolom: kolom teks pertama & kolom Rupiah lebih lebar.
        bobot = [1.8 if (i == 0 and t == "teks") else 1.4 if t == "rp" else 1.0
                 for i, t in enumerate(b["tipe"])]
        col_w = [lebar * w / sum(bobot) for w in bobot]
        data = [[Paragraph(escape(k), s_head) for k in b["kolom"]]]
        for r in b["baris"]:
            data.append([Paragraph(escape(format_nilai(v, t)), s_sel if t == "teks" else s_sel_r)
                         for v, t in zip(r, b["tipe"])])
        t = Table(data, colWidths=col_w, repeatRows=1)
        gaya = [("BACKGROUND", (0, 0), (-1, 0), biru),
                ("BOX", (0, 0), (-1, -1), 0.5, garis),
                ("LINEBELOW", (0, 0), (-1, -1), 0.25, garis),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]
        for i in range(2, len(data), 2):
            gaya.append(("BACKGROUND", (0, i), (-1, i), abu))
        t.setStyle(TableStyle(gaya))
        isi = [judul, t]
        if b["catatan"]:
            isi.append(Paragraph(escape(b["catatan"]), s_cat))
        # Tabel pendek dijaga satu halaman dengan judulnya; tabel panjang boleh terpotong
        # (header tabel diulang lewat repeatRows).
        if len(data) <= 25:
            el.append(KeepTogether(isi))
        else:
            el += isi
    doc.build(el, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


# --------------------------------------------------------------------------- XLSX
_FORMAT_XLSX = {"int": "#,##0", "rp": '"Rp" #,##0', "persen": '0.0"%"', "desimal": "#,##0.00"}


def render_xlsx(lap: dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Ringkasan"
    tebal = Font(bold=True)
    head_fill = PatternFill("solid", fgColor="1E3A8A")
    head_font = Font(bold=True, color="FFFFFF")

    ws["A1"] = lap["judul"]
    ws["A1"].font = Font(bold=True, size=15, color="1E3A8A")
    ws["A2"] = lap["subjudul"]
    ws["A2"].font = Font(bold=True, size=12)
    r = 4
    for k, v in lap["meta"]:
        ws.cell(r, 1, k).font = tebal
        ws.cell(r, 2, v)
        r += 1
    if lap["kpi"]:
        r += 1
        ws.cell(r, 1, "Indikator Utama Transaksi").font = Font(bold=True, size=12,
                                                               color="1E3A8A")
        r += 1
        for k, v, t in lap["kpi"]:
            ws.cell(r, 1, k)
            c = ws.cell(r, 2, v)
            c.number_format = _FORMAT_XLSX.get(t, "General")
            r += 1
    r += 1
    ws.cell(r, 1, "Isi laporan").font = Font(bold=True, size=12, color="1E3A8A")
    r += 1
    for b in lap["bagian"]:
        ws.cell(r, 1, b["judul"])
        ws.cell(r, 2, b["error"] or f"lihat sheet '{_nama_sheet(b['judul'])}'")
        r += 1
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 48

    dipakai = {"Ringkasan"}
    for b in lap["bagian"]:
        nama = _nama_sheet(b["judul"])
        while nama in dipakai:
            nama = nama[:28] + str(len(dipakai))
        dipakai.add(nama)
        s = wb.create_sheet(nama)
        s["A1"] = b["judul"]
        s["A1"].font = Font(bold=True, size=13, color="1E3A8A")
        if b["error"]:
            s["A3"] = b["error"]
            s.column_dimensions["A"].width = 80
            continue
        for j, k in enumerate(b["kolom"], start=1):
            c = s.cell(3, j, k)
            c.fill, c.font = head_fill, head_font
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for i, baris in enumerate(b["baris"], start=4):
            for j, (v, t) in enumerate(zip(baris, b["tipe"]), start=1):
                c = s.cell(i, j, v if v is not None else "-")
                if t in _FORMAT_XLSX and isinstance(v, (int, float)):
                    c.number_format = _FORMAT_XLSX[t]
        if b["catatan"]:
            s.cell(len(b["baris"]) + 5, 1, b["catatan"]).font = Font(italic=True,
                                                                    color="64748B")
        for j, k in enumerate(b["kolom"], start=1):
            panjang = max([len(str(k))] + [len(format_nilai(x[j - 1], b["tipe"][j - 1]))
                                           for x in b["baris"]])
            s.column_dimensions[get_column_letter(j)].width = min(45, max(12, panjang + 3))
        s.freeze_panes = "A4"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _nama_sheet(judul: str) -> str:
    bersih = "".join(ch for ch in judul if ch not in '[]:*?/\\')
    return bersih[:31]


def buat_file(db: Session, jenis: str, fmt: str, mulai: date | None, selesai: date | None,
              cabang: str | None, dibuat_oleh: str) -> tuple[bytes, str, str]:
    """Kembalikan (isi_file, nama_file, media_type)."""
    if fmt not in FORMAT_LAPORAN:
        raise ValueError("Format harus pdf atau xlsx")
    lap = susun_laporan(db, jenis, mulai, selesai, cabang, dibuat_oleh)
    stamp = datetime.now(WIB).strftime("%Y%m%d_%H%M")
    bag_cabang = f"_{cabang.lower().replace(' ', '-')}" if cabang else ""
    nama = f"laporan_{jenis}{bag_cabang}_{stamp}.{fmt}"
    if fmt == "pdf":
        return render_pdf(lap), nama, "application/pdf"
    return (render_xlsx(lap), nama,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
