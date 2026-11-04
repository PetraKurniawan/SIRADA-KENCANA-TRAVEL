"""
Load data transaksi (hasil generator sintetis sebelumnya) ke database,
dinormalisasi sesuai skema ERD: Cabang, Rute, Armada, Member, JenisPaket, Kalender, Jadwal,
Transaksi.

Jalankan: python -m app.load_data /path/ke/kencana_transaksi_gabungan.csv
"""
import sys
import random
import pandas as pd
from datetime import datetime, timedelta
from sqlalchemy import text
from app.core.database import engine, SessionLocal, Base
from app.models.models import Cabang, Rute, Armada, Member, JenisPaket, Transaksi
from app.services.referensi_service import pastikan_jadwal, pastikan_kalender

NAMA_DEPAN = ["Ahmad", "Siti", "Budi", "Dewi", "Agus", "Rina", "Hendro", "Ratna",
              "Bambang", "Wahyuni", "Fauzi", "Kartika", "Nugroho", "Putri", "Santoso", "Lestari"]
NAMA_BELAKANG = ["Wijaya", "Nurhaliza", "Prabowo", "Santoso", "Kartika", "Wahyuni",
                 "Nugroho", "Lestari", "Hidayat", "Susanti", "Setiawan", "Permata"]


CHECKPOINT_TIAP = 50_000  # baris


def _checkpoint(db):
    """PostgreSQL: paksa CHECKPOINT agar file WAL lama bisa didaur ulang. Tanpa ini, WAL
    dari bulk insert menumpuk dan bisa memenuhi volume kecil (mis. 0,5 GB di trial Railway).
    Diabaikan untuk SQLite atau bila user database tidak punya hak CHECKPOINT."""
    if db.bind.dialect.name != "postgresql":
        return
    try:
        db.execute(text("CHECKPOINT"))
        db.commit()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        print(f"  (CHECKPOINT dilewati: {e.__class__.__name__})", flush=True)


def buat_nama_acak(seed_str):
    rnd = random.Random(seed_str)
    return f"{rnd.choice(NAMA_DEPAN)} {rnd.choice(NAMA_BELAKANG)}"


def main(csv_path, reset=True):
    """reset=True: HAPUS semua tabel lalu buat ulang (termasuk akun pengguna!).
    reset=False: isi ke tabel yang sudah ada — dipakai app.bootstrap untuk database kosong."""
    print(f"Membaca {csv_path} ...")
    df = pd.read_csv(csv_path)
    print(f"Total baris sumber: {len(df):,}")

    if reset:
        Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()

    # --- 1. Cabang ---
    cabang_kota = {"Solo": "Surakarta", "Semarang": "Semarang", "Tayu": "Pati"}
    cabang_map = {}
    for nama, kota in cabang_kota.items():
        c = Cabang(nama_cabang=nama, kota=kota)
        db.add(c)
        db.flush()
        cabang_map[nama] = c.id_cabang
    print("Cabang:", list(cabang_map.keys()))

    # --- 2. Rute ---
    rute_map = {}
    for nama_rute, grp in df.groupby("Rute"):
        asal, tujuan = nama_rute.split("->")
        layanan_tersedia = ",".join(sorted(grp["Layanan"].unique()))
        r = Rute(nama_rute=nama_rute, cabang_asal=asal, cabang_tujuan=tujuan,
                  layanan_tersedia=layanan_tersedia)
        db.add(r)
        db.flush()
        rute_map[nama_rute] = r.id_rute
    print("Rute:", list(rute_map.keys()))

    # --- 3. Armada ---
    armada_map = {}
    for kode, grp in df.groupby("Armada"):
        layanan = grp["Layanan"].iloc[0]
        kapasitas = 8 if layanan == "VIP" else 12
        basis = kode.split("-")[0]
        basis_nama = {"SOLO": "Solo", "SMG": "Semarang", "TAYU": "Tayu"}.get(basis, "Semarang")
        jenis_kendaraan = "Toyota HiAce Premio" if layanan == "VIP" else "Toyota HiAce Commuter"
        a = Armada(kode_armada=kode, jenis_kendaraan=jenis_kendaraan, tipe_layanan=layanan,
                   kapasitas=kapasitas, basis_outlet=basis_nama)
        db.add(a)
        db.flush()
        armada_map[kode] = a.id_armada
    print(f"Armada: {len(armada_map)} unit")

    # --- 4. Member ---
    member_map = {}
    diskon_map = {"Non-Member": 0, "Member Umum": 10000, "Member Mahasiswa": 15000, "-": 0}
    for jenis in df["Jenis Member"].dropna().unique():
        m = Member(jenis_member=jenis, diskon_per_tiket=diskon_map.get(jenis, 0))
        db.add(m)
        db.flush()
        member_map[jenis] = m.id_member
    print("Member:", list(member_map.keys()))

    # --- 5. JenisPaket ---
    paket_map = {}
    tarif = {"Reguler": (25000, 5000), "Elektronik": (40000, 5000)}
    for jenis in df["Jenis Paket"].dropna().unique():
        if jenis == "-":
            continue
        dasar, lanjut = tarif.get(jenis, (25000, 5000))
        p = JenisPaket(nama_paket=jenis, tarif_5kg_pertama=dasar, tarif_per_kg_lanjut=lanjut)
        db.add(p)
        db.flush()
        paket_map[jenis] = p.id_jenis_paket
    print("JenisPaket:", list(paket_map.keys()))

    db.commit()

    # --- 6. Kalender & Jadwal (dirujuk foreign key transaksi, jadi diisi lebih dulu) ---
    tanggal = pd.to_datetime(df["Tanggal"])
    pastikan_kalender(db, tanggal.min().date(), tanggal.max().date() + timedelta(days=400))
    kombinasi = df[["Rute", "Jam Keberangkatan", "Layanan"]].drop_duplicates()
    n_jadwal = pastikan_jadwal(db, ((rute_map[r], j, l) for r, j, l in kombinasi.itertuples(index=False)))
    db.commit()
    print(f"Kalender {tanggal.min().date()} s.d. +400 hari, Jadwal: {n_jadwal}")

    # --- 7. Transaksi (bulk insert, batched) ---
    print("Memuat transaksi (bulk insert)...")
    records = []
    BATCH = 5000
    total = 0
    for row in df.itertuples(index=False, name=None):
        d = dict(zip(df.columns, row))
        member_val = d.get("Jenis Member")
        paket_val = d.get("Jenis Paket")
        nama = None
        if d["Jenis Transaksi"] == "Penumpang":
            # Dataset terbaru pakai "ID Pelanggan", dataset lama pakai "Nama Pelanggan".
            # Bila keduanya absen, pakai nama acak.
            nama_csv = d.get("ID Pelanggan") or d.get("Nama Pelanggan")
            nama = nama_csv if isinstance(nama_csv, str) and nama_csv.strip() else \
                   buat_nama_acak(d["Kode Transaksi"])
        rec = Transaksi(
            id_transaksi=d["Kode Transaksi"],
            tanggal=datetime.strptime(d["Tanggal"], "%Y-%m-%d").date(),
            hari=d["Hari"],
            jam_keberangkatan=d["Jam Keberangkatan"],
            id_rute=rute_map[d["Rute"]],
            id_armada=armada_map.get(d["Armada"]),
            cabang_asal=d["Cabang Asal"],
            cabang_tujuan=d["Cabang Tujuan"],
            layanan=d["Layanan"],
            jenis_transaksi=d["Jenis Transaksi"],
            id_member=member_map.get(member_val) if member_val and member_val != "-" else None,
            id_jenis_paket=paket_map.get(paket_val) if paket_val and paket_val != "-" else None,
            jumlah_unit=d["Jumlah Unit"],
            satuan=d["Satuan"],
            channel_pemesanan=d["Channel Pemesanan"],
            harga_satuan=d["Harga Satuan"],
            diskon_per_tiket=d["Diskon per Tiket"],
            total_harga=d["Total Harga"],
            total_diskon=d["Total Diskon"],
            total_bayar=d["Total Bayar"],
            keterangan=d["Keterangan"],
            id_pelanggan=nama,
        )
        records.append(rec)
        if len(records) >= BATCH:
            db.bulk_save_objects(records)
            db.commit()
            total += len(records)
            print(f"  ... {total:,} baris dimuat", flush=True)
            records = []
            if total % CHECKPOINT_TIAP == 0:
                _checkpoint(db)
    if records:
        db.bulk_save_objects(records)
        db.commit()
        total += len(records)
    print(f"Selesai. Total transaksi dimuat: {total:,}")

    # Seed akun login: jalankan terpisah dengan `python -m app.seed_users`
    db.close()


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "kencana_transaksi_gabungan.csv"
    main(path)
