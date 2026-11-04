"""Paket aplikasi Kencana Analytics API.

Muat variabel dari file .env (bila ada) sebelum modul lain membaca os.getenv,
supaya konfigurasi yang sama berlaku untuk server, seed_users, dan load_data.
Di Railway, variabel diisi lewat dashboard dan .env tidak diperlukan."""
from dotenv import load_dotenv

load_dotenv()
