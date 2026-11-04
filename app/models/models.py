"""
Model tabel sesuai ERD: Cabang, Rute, Armada, Jadwal, Kalender, Member, JenisPaket,
Transaksi, ditambah tabel pendukung sistem: Pengguna, TokenPengguna, PermintaanLaporan,
RiwayatImport.
"""
from sqlalchemy import (Column, String, Integer, Float, DateTime, ForeignKey, Date, Boolean,
                        ForeignKeyConstraint, UniqueConstraint)
from sqlalchemy.orm import relationship
from app.core.database import Base
from datetime import datetime, timezone
import uuid


def gen_id():
    return uuid.uuid4().hex[:12]


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Pengguna(Base):
    """Akun login sistem (Admin, Owner, Kepala Outlet) - untuk kebutuhan auth,
    terpisah dari entitas bisnis di ERD."""
    __tablename__ = "pengguna"
    id_pengguna = Column(String, primary_key=True, default=gen_id)
    nama = Column(String, nullable=False)
    email = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    # Admin | Owner | KepalaOutlet. Kosong ("") untuk pendaftar yang belum disetujui Admin.
    role = Column(String, nullable=False)
    id_cabang = Column(String, ForeignKey("cabang.id_cabang"), nullable=True)  # khusus KepalaOutlet
    email_terverifikasi = Column(Boolean, nullable=False, default=False, server_default="1")
    # menunggu (daftar sendiri, belum di-approve) | aktif | ditolak
    status = Column(String, nullable=False, default="menunggu", server_default="aktif")
    dibuat_pada = Column(DateTime, nullable=True, default=utcnow)


class TokenPengguna(Base):
    """Token sekali pakai untuk link di email. Yang disimpan hanya hash SHA-256-nya,
    jadi isi database yang bocor tidak bisa dipakai untuk verifikasi/reset."""
    __tablename__ = "token_pengguna"
    id_token = Column(String, primary_key=True, default=gen_id)
    id_pengguna = Column(String, ForeignKey("pengguna.id_pengguna"), nullable=False, index=True)
    token_hash = Column(String, unique=True, nullable=False, index=True)
    jenis = Column(String, nullable=False)  # verifikasi_email | reset_password
    kedaluwarsa = Column(DateTime, nullable=False)
    dipakai_pada = Column(DateTime, nullable=True)
    dibuat_pada = Column(DateTime, nullable=False, default=utcnow)


class Cabang(Base):
    __tablename__ = "cabang"
    id_cabang = Column(String, primary_key=True, default=gen_id)
    nama_cabang = Column(String, unique=True, nullable=False)
    kota = Column(String, nullable=False)


class Rute(Base):
    __tablename__ = "rute"
    id_rute = Column(String, primary_key=True, default=gen_id)
    nama_rute = Column(String, unique=True, nullable=False)  # e.g. "Solo->Semarang"
    cabang_asal = Column(String, ForeignKey("cabang.nama_cabang"), nullable=False)
    cabang_tujuan = Column(String, ForeignKey("cabang.nama_cabang"), nullable=False)
    layanan_tersedia = Column(String, nullable=False)  # "VIP" / "Reguler" / "VIP,Reguler"


class Armada(Base):
    __tablename__ = "armada"
    id_armada = Column(String, primary_key=True, default=gen_id)
    kode_armada = Column(String, unique=True, nullable=False)
    jenis_kendaraan = Column(String, nullable=False)
    tipe_layanan = Column(String, nullable=False)
    kapasitas = Column(Integer, nullable=False)
    basis_outlet = Column(String, ForeignKey("cabang.nama_cabang"), nullable=True)


class Jadwal(Base):
    """Satu jadwal = satu kombinasi rute + jam keberangkatan + layanan. Transaksi merujuk ke
    jadwal lewat foreign key gabungan (id_rute, jam_keberangkatan, layanan) supaya relasi ini
    bisa ditambahkan ke database produksi tanpa menulis ulang seluruh tabel transaksi."""
    __tablename__ = "jadwal"
    __table_args__ = (UniqueConstraint("id_rute", "jam_keberangkatan", "layanan",
                                       name="uq_jadwal_rute_jam_layanan"),)
    id_jadwal = Column(String, primary_key=True, default=gen_id)
    id_rute = Column(String, ForeignKey("rute.id_rute"), nullable=False)
    jam_keberangkatan = Column(String, nullable=False)
    layanan = Column(String, nullable=False)
    hari_berlaku = Column(String, default="Setiap Hari")


class Kalender(Base):
    """Dimensi tanggal: hari, akhir pekan, libur nasional/cuti bersama, dan libur sekolah.
    Dipakai sebagai variabel eksternal forecasting dan untuk EDA."""
    __tablename__ = "kalender"
    tanggal = Column(Date, primary_key=True)
    hari = Column(String, nullable=False)
    weekend = Column(Boolean, nullable=False, default=False)
    libur_nasional = Column(Boolean, nullable=False, default=False)
    libur_sekolah = Column(Boolean, nullable=False, default=False)
    keterangan = Column(String, nullable=True)


class Member(Base):
    __tablename__ = "member"
    id_member = Column(String, primary_key=True, default=gen_id)
    jenis_member = Column(String, unique=True, nullable=False)  # Non-Member/Member Umum/Member Mahasiswa
    diskon_per_tiket = Column(Float, default=0)


class JenisPaket(Base):
    __tablename__ = "jenis_paket"
    id_jenis_paket = Column(String, primary_key=True, default=gen_id)
    nama_paket = Column(String, unique=True, nullable=False)  # Reguler / Elektronik
    tarif_5kg_pertama = Column(Float, nullable=False)
    tarif_per_kg_lanjut = Column(Float, nullable=False)


class Transaksi(Base):
    __tablename__ = "transaksi"
    __table_args__ = (
        ForeignKeyConstraint(["id_rute", "jam_keberangkatan", "layanan"],
                             ["jadwal.id_rute", "jadwal.jam_keberangkatan", "jadwal.layanan"],
                             name="fk_transaksi_jadwal"),
    )
    # Index hanya di kolom yang selektif (PK, tanggal, id_rute). Kolom seperti layanan/channel
    # hanya punya 2-3 nilai, jadi index-nya tidak dipakai query tapi memakan belasan MB per
    # index dan memperbesar WAL saat bulk insert (volume Postgres trial Railway hanya ~0,5 GB).
    id_transaksi = Column(String, primary_key=True)   # kode booking (KCN-xxxxx / SLOxxxxxx dst)
    tanggal = Column(Date, ForeignKey("kalender.tanggal", name="fk_transaksi_kalender"),
                     nullable=False, index=True)
    hari = Column(String)
    jam_keberangkatan = Column(String, nullable=False)
    id_rute = Column(String, ForeignKey("rute.id_rute"), nullable=False, index=True)
    id_armada = Column(String, ForeignKey("armada.id_armada"), nullable=True)
    cabang_asal = Column(String, nullable=False)
    cabang_tujuan = Column(String, nullable=False)
    layanan = Column(String, nullable=False)
    jenis_transaksi = Column(String, nullable=False)  # Penumpang / Paket
    id_member = Column(String, ForeignKey("member.id_member"), nullable=True)
    id_jenis_paket = Column(String, ForeignKey("jenis_paket.id_jenis_paket"), nullable=True)
    jumlah_unit = Column(Float, nullable=False)  # jml orang ATAU berat kg
    satuan = Column(String, nullable=False)
    channel_pemesanan = Column(String, nullable=False)
    harga_satuan = Column(Float, nullable=False)
    diskon_per_tiket = Column(Float, default=0)
    total_harga = Column(Float, nullable=False)
    total_diskon = Column(Float, default=0)
    total_bayar = Column(Float, nullable=False)
    keterangan = Column(String)
    # ID pelanggan anonim (proxy ID untuk RFM/segmentasi). Menggantikan nama_pelanggan.
    id_pelanggan = Column(String, nullable=True)
    # Legacy: dikosongkan oleh migrasi (kebijakan anonimisasi); tidak lagi dipakai aplikasi.
    nama_pelanggan = Column(String, nullable=True)

    rute = relationship("Rute")
    armada = relationship("Armada")
    member = relationship("Member")
    jenis_paket_rel = relationship("JenisPaket")


class PermintaanLaporan(Base):
    """Owner / Kepala Outlet mengajukan permintaan laporan strategis, Admin memprosesnya."""
    __tablename__ = "permintaan_laporan"
    id_permintaan = Column(String, primary_key=True, default=gen_id)
    id_pemohon = Column(String, ForeignKey("pengguna.id_pengguna"), nullable=False, index=True)
    jenis_laporan = Column(String, nullable=False)  # ringkasan | segmentasi | forecasting | performa
    tanggal_mulai = Column(Date, nullable=True)
    tanggal_selesai = Column(Date, nullable=True)
    cabang = Column(String, ForeignKey("cabang.nama_cabang"), nullable=True)
    catatan = Column(String, nullable=True)
    status = Column(String, nullable=False, default="menunggu")  # menunggu | selesai | ditolak
    catatan_admin = Column(String, nullable=True)
    diproses_oleh = Column(String, ForeignKey("pengguna.id_pengguna"), nullable=True)
    dibuat_pada = Column(DateTime, nullable=False, default=utcnow)
    diproses_pada = Column(DateTime, nullable=True)


class RiwayatImport(Base):
    """Log setiap import CSV beserta hasil data cleaning-nya."""
    __tablename__ = "riwayat_import"
    id_import = Column(String, primary_key=True, default=gen_id)
    nama_file = Column(String, nullable=False)
    diimport_oleh = Column(String, ForeignKey("pengguna.id_pengguna"), nullable=True)
    waktu = Column(DateTime, nullable=False, default=utcnow)
    baris_sumber = Column(Integer, nullable=False, default=0)
    baris_duplikat = Column(Integer, nullable=False, default=0)      # duplikat di dalam file
    baris_sudah_ada = Column(Integer, nullable=False, default=0)     # kode transaksi sudah di DB
    baris_kosong = Column(Integer, nullable=False, default=0)        # kolom wajib kosong
    baris_tidak_valid = Column(Integer, nullable=False, default=0)   # format/nilai tidak valid
    baris_dimuat = Column(Integer, nullable=False, default=0)
    status = Column(String, nullable=False, default="berhasil")      # berhasil | gagal
    catatan = Column(String, nullable=True)


class InfoSistem(Base):
    """Pasangan kunci-nilai untuk status sistem, mis. sidik jari (hash) file CSV dataset yang
    sedang dimuat, agar bootstrap tahu kapan dataset di repo berganti."""
    __tablename__ = "info_sistem"
    kunci = Column(String, primary_key=True)
    nilai = Column(String, nullable=True)


class TindakLanjut(Base):
    """Tindakan lanjut yang perlu diambil berdasarkan temuan analisis rute."""
    __tablename__ = "tindak_lanjut"
    id = Column(String, primary_key=True, default=gen_id)
    id_rute = Column(String, ForeignKey("rute.id_rute"), nullable=True, index=True)
    sumber = Column(String, nullable=True)  # halaman asal, mis. "analisis-terpadu"
    judul = Column(String, nullable=False)
    deskripsi = Column(String, nullable=True)
    prioritas = Column(String, nullable=False, default="sedang")  # tinggi | sedang | rendah
    # baru | diproses | selesai | ditolak
    status = Column(String, nullable=False, default="baru", index=True)
    id_pembuat = Column(String, ForeignKey("pengguna.id_pengguna"), nullable=False, index=True)
    catatan = Column(String, nullable=True)
    tenggat = Column(Date, nullable=True)
    dibuat_pada = Column(DateTime, nullable=False, default=utcnow)
    selesai_pada = Column(DateTime, nullable=True)

    rute = relationship("Rute")
    pembuat = relationship("Pengguna", foreign_keys=[id_pembuat])

