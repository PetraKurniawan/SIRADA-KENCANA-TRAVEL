"""Koneksi database. Default: SQLite file lokal (kencana.db).
Untuk produksi tinggal ganti DATABASE_URL ke PostgreSQL, tidak ada kode lain yang berubah
karena semua akses lewat SQLAlchemy ORM."""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./kencana.db")
# Railway memberi URL "postgresql://" (kadang "postgres://") tanpa nama driver. Driver
# default SQLAlchemy berubah antarversi (2.1 memakai psycopg v3), jadi tetapkan psycopg2
# secara eksplisit sesuai paket di requirements.txt.
for _skema in ("postgres://", "postgresql://"):
    if DATABASE_URL.startswith(_skema):
        DATABASE_URL = "postgresql+psycopg2://" + DATABASE_URL[len(_skema):]
        break

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
# pool_pre_ping: buang koneksi Postgres yang sudah diputus server sebelum dipakai.
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
