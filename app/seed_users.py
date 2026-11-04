"""Seed akun login awal (hanya bila tabel pengguna masih kosong).

Jalankan: python -m app.seed_users

- Bila ADMIN_EMAIL & ADMIN_PASSWORD di-set (disarankan untuk produksi): hanya membuat
  1 akun Admin dengan email asli tsb, sehingga fitur lupa password bisa dipakai.
- Bila tidak: membuat 3 akun demo dengan password lemah (untuk pengembangan lokal)."""
import os
from app.core.database import SessionLocal
from app.core.security import hash_password
from app.models.models import Pengguna, Cabang


def seed() -> None:
    db = SessionLocal()
    try:
        if db.query(Pengguna).first():
            print("Akun sudah ada, tidak membuat ulang.")
            return

        admin_email = os.getenv("ADMIN_EMAIL", "").strip().lower()
        admin_password = os.getenv("ADMIN_PASSWORD", "")
        if admin_email and admin_password:
            if len(admin_password) < 8:
                raise SystemExit("ADMIN_PASSWORD minimal 8 karakter.")
            db.add(Pengguna(nama=os.getenv("ADMIN_NAMA", "Admin Kencana"), email=admin_email,
                            password_hash=hash_password(admin_password), role="Admin",
                            email_terverifikasi=True, status="aktif"))
            db.commit()
            print(f"Akun Admin dibuat: {admin_email}")
            return

        solo = db.query(Cabang).filter(Cabang.nama_cabang == "Solo").first()
        db.add_all([
            Pengguna(nama="Admin Kencana", email="admin@kencanatravel.co.id",
                     password_hash=hash_password("admin123"), role="Admin", email_terverifikasi=True, status="aktif"),
            Pengguna(nama="Owner Kencana", email="owner@kencanatravel.co.id",
                     password_hash=hash_password("owner123"), role="Owner", email_terverifikasi=True, status="aktif"),
            Pengguna(nama="Kepala Outlet Solo", email="kepala.solo@kencanatravel.co.id",
                     password_hash=hash_password("kepala123"), role="KepalaOutlet", email_terverifikasi=True, status="aktif",
                     id_cabang=solo.id_cabang if solo else None),
        ])
        db.commit()
        print("Akun DEMO dibuat (password lemah — jangan dipakai di produksi):")
        print("  admin@kencanatravel.co.id / admin123 (Admin)")
        print("  owner@kencanatravel.co.id / owner123 (Owner)")
        print("  kepala.solo@kencanatravel.co.id / kepala123 (KepalaOutlet)")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
