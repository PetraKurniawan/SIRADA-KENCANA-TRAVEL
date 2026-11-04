"""Konfigurasi email & URL frontend, dibaca dari environment variable."""
import os

# Penyedia email (dipilih otomatis dari API key yang diisi, Brevo didahulukan):
# - Brevo (https://app.brevo.com -> SMTP & API -> API Keys). Alamat di MAIL_FROM harus sudah
#   diverifikasi sebagai sender di Brevo, tapi tidak perlu punya domain sendiri.
# - Resend (lama). Tanpa domain terverifikasi hanya bisa mengirim ke email pemilik akun.
# Keduanya kosong = mode dev: email tidak dikirim, isi & link-nya dicetak ke log server.
BREVO_API_KEY = os.getenv("BREVO_API_KEY", "")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")

# Alamat pengirim, format "Nama <email>". Untuk Brevo: email sender yang sudah diverifikasi.
MAIL_FROM = os.getenv("MAIL_FROM", "SIRADA Kencana <onboarding@resend.dev>")

# Dipakai untuk membangun link di dalam email (verifikasi & reset password).
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173").rstrip("/")

VERIFIKASI_EMAIL_EXPIRE_JAM = 24
RESET_PASSWORD_EXPIRE_MENIT = 30

# Syarat minimal jumlah baris transaksi sebelum analisis boleh dijalankan (flowchart:
# "data lengkap & jumlah >= 100.000 baris").
MIN_BARIS_ANALISIS = int(os.getenv("MIN_BARIS_ANALISIS", "1000"))
