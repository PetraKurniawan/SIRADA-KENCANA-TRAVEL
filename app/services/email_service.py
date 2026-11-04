"""Pengiriman email transaksional (verifikasi, reset password, status akun) via Brevo
(atau Resend bila hanya RESEND_API_KEY yang diisi)."""
import html
import json
import logging
import urllib.error
import urllib.request
from email.utils import parseaddr

import resend
from app.core import config

log = logging.getLogger("uvicorn.error")

WARNA_UTAMA = "#7A1F2B"


def _template(judul: str, paragraf: list[str], tombol_teks: str | None = None,
              tombol_url: str | None = None, catatan: str | None = None) -> str:
    isi = "".join(f'<p style="margin:0 0 14px;line-height:1.6">{p}</p>' for p in paragraf)
    tombol = ""
    if tombol_teks and tombol_url:
        tombol = (
            f'<p style="margin:24px 0"><a href="{html.escape(tombol_url)}" '
            f'style="background:{WARNA_UTAMA};color:#fff;text-decoration:none;padding:12px 22px;'
            f'border-radius:8px;font-weight:600;display:inline-block">{tombol_teks}</a></p>'
            f'<p style="margin:0 0 14px;font-size:12px;color:#64748b;line-height:1.6">'
            f'Jika tombol tidak berfungsi, salin link ini ke browser:<br>'
            f'<span style="word-break:break-all">{html.escape(tombol_url)}</span></p>'
        )
    catatan_html = (f'<p style="margin:16px 0 0;font-size:12px;color:#64748b">{catatan}</p>'
                    if catatan else "")
    return f"""\
<div style="background:#f8f9fa;padding:32px 16px;font-family:Segoe UI,Arial,sans-serif;color:#0f172a">
  <div style="max-width:520px;margin:0 auto;background:#fff;border:1px solid #e5e7eb;border-radius:12px;overflow:hidden">
    <div style="background:{WARNA_UTAMA};color:#fff;padding:18px 24px;font-weight:800;font-size:18px">
      SIRADA Kencana
    </div>
    <div style="padding:24px">
      <h1 style="margin:0 0 16px;font-size:20px">{judul}</h1>
      {isi}{tombol}{catatan_html}
    </div>
  </div>
  <p style="text-align:center;font-size:11px;color:#94a3b8;margin-top:16px">
    Email otomatis dari SIRADA Kencana — Kencana Travel. Mohon tidak membalas email ini.
  </p>
</div>"""


BREVO_URL = "https://api.brevo.com/v3/smtp/email"


def _kirim_brevo(ke: str, subjek: str, isi_html: str) -> None:
    nama_pengirim, email_pengirim = parseaddr(config.MAIL_FROM)
    data = json.dumps({
        "sender": {"name": nama_pengirim or "SIRADA Kencana", "email": email_pengirim},
        "to": [{"email": ke}],
        "subject": subjek,
        "htmlContent": isi_html,
    }).encode()
    req = urllib.request.Request(BREVO_URL, data=data, method="POST", headers={
        "api-key": config.BREVO_API_KEY, "content-type": "application/json",
        "accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            log.info("Email terkirim via Brevo ke %s (%s): %s", ke, subjek, resp.read().decode()[:200])
    except urllib.error.HTTPError as e:
        # Pesan Brevo menjelaskan penyebabnya (sender belum diverifikasi, IP belum diizinkan, dll.)
        raise RuntimeError(f"Brevo HTTP {e.code}: {e.read().decode(errors='replace')[:500]}") from e


def kirim_email(ke: str, subjek: str, isi_html: str, link: str | None = None) -> None:
    """Kirim email. Dipanggil lewat BackgroundTasks, jadi kegagalan hanya dicatat ke log
    (tidak membatalkan request) — pengguna bisa meminta kirim ulang."""
    if not config.BREVO_API_KEY and not config.RESEND_API_KEY:
        log.warning("[EMAIL DEV MODE] BREVO_API_KEY/RESEND_API_KEY kosong, email tidak dikirim.\n"
                    "  Ke: %s\n  Subjek: %s\n  Link: %s", ke, subjek, link or "-")
        return
    try:
        if config.BREVO_API_KEY:
            _kirim_brevo(ke, subjek, isi_html)
        else:
            resend.api_key = config.RESEND_API_KEY
            resend.Emails.send({"from": config.MAIL_FROM, "to": [ke], "subject": subjek,
                                "html": isi_html})
    except Exception:
        log.exception("Gagal mengirim email ke %s (subjek: %s)", ke, subjek)


def kirim_verifikasi_email(ke: str, nama: str, token: str) -> None:
    url = f"{config.FRONTEND_URL}/verifikasi-email?token={token}"
    kirim_email(ke, "Verifikasi email akun SIRADA Kencana", _template(
        "Verifikasi email Anda",
        [f"Halo {html.escape(nama)},",
         "Terima kasih telah mendaftar di SIRADA Kencana. Klik tombol di bawah untuk "
         "memverifikasi alamat email Anda. Setelah itu, akun akan ditinjau oleh Admin sebelum bisa dipakai."],
        "Verifikasi Email", url,
        f"Link berlaku {config.VERIFIKASI_EMAIL_EXPIRE_JAM} jam. Abaikan email ini jika Anda tidak merasa mendaftar.",
    ), link=url)


def kirim_reset_password(ke: str, nama: str, token: str) -> None:
    url = f"{config.FRONTEND_URL}/reset-password?token={token}"
    kirim_email(ke, "Reset kata sandi SIRADA Kencana", _template(
        "Reset kata sandi",
        [f"Halo {html.escape(nama)},",
         "Kami menerima permintaan untuk mengatur ulang kata sandi akun Anda. "
         "Klik tombol di bawah untuk membuat kata sandi baru."],
        "Buat Kata Sandi Baru", url,
        f"Link berlaku {config.RESET_PASSWORD_EXPIRE_MENIT} menit dan hanya bisa dipakai sekali. "
        "Abaikan email ini jika Anda tidak meminta reset — kata sandi Anda tidak berubah.",
    ), link=url)


def kirim_akun_disetujui(ke: str, nama: str, role: str) -> None:
    kirim_email(ke, "Akun SIRADA Kencana Anda telah aktif", _template(
        "Akun Anda telah disetujui",
        [f"Halo {html.escape(nama)},",
         f"Admin telah menyetujui akun Anda dengan peran <strong>{html.escape(role)}</strong>. "
         "Sekarang Anda bisa masuk ke dashboard."],
        "Masuk ke SIRADA", f"{config.FRONTEND_URL}/login",
    ))


def kirim_akun_ditolak(ke: str, nama: str) -> None:
    kirim_email(ke, "Pendaftaran akun SIRADA Kencana", _template(
        "Pendaftaran tidak disetujui",
        [f"Halo {html.escape(nama)},",
         "Mohon maaf, pendaftaran akun Anda di SIRADA Kencana tidak disetujui oleh Admin. "
         "Hubungi Admin atau tim IT Kencana Travel jika menurut Anda ini keliru."],
    ))
