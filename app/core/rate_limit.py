"""Rate limit sederhana (in-memory, sliding window).

Dipakai untuk:
- endpoint yang mengirim email (daftar, kirim ulang verifikasi, lupa password)
- percobaan login yang GAGAL (mencegah tebak-tebakan password)

Catatan: hitungan disimpan di memori proses, jadi reset saat server restart dan tidak
dibagi antar-instance. Cukup untuk 1 instance di Railway; bila nanti di-scale ke
beberapa replika, pindahkan penyimpanan ke Redis/database."""
import math
import time
from collections import defaultdict, deque
from threading import Lock
from fastapi import HTTPException, Request

# Endpoint pengirim email: maks 5 permintaan per jam, per email & per IP.
BATAS_PERMINTAAN = 5
JENDELA_DETIK = 60 * 60

# Login: maks 5 password salah per 15 menit untuk satu email. Per IP dibuat lebih longgar
# karena satu outlet biasanya berbagi satu IP internet.
BATAS_LOGIN_GAGAL_EMAIL = 5
BATAS_LOGIN_GAGAL_IP = 20
JENDELA_LOGIN_DETIK = 15 * 60

_riwayat: dict[str, deque] = defaultdict(deque)
_lock = Lock()


def ip_klien(request: Request) -> str:
    # Di belakang proxy (Railway), IP asli ada di X-Forwarded-For. Ambil entri paling kanan
    # (ditambahkan proxy) — entri kiri bisa dipalsukan oleh klien.
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def _antrian(kunci: str, jendela: int, sekarang: float) -> deque:
    """Ambil riwayat kunci & buang entri yang sudah di luar jendela. Panggil di dalam _lock."""
    q = _riwayat[kunci]
    while q and sekarang - q[0] >= jendela:
        q.popleft()
    return q


def _tolak(q: deque, jendela: int, sekarang: float, pesan: str):
    sisa_menit = max(1, math.ceil((jendela - (sekarang - q[0])) / 60))
    raise HTTPException(status_code=429, detail=pesan.format(menit=sisa_menit))


def cek_rate_limit(aksi: str, *kunci: str) -> None:
    """Tolak dengan 429 bila salah satu kunci (mis. email, IP) sudah mencapai batas
    untuk aksi ini dalam jendela waktu. Permintaan yang lolos langsung dicatat."""
    sekarang = time.monotonic()
    with _lock:
        antrian = []
        for k in kunci:
            q = _antrian(f"{aksi}:{k}", JENDELA_DETIK, sekarang)
            if len(q) >= BATAS_PERMINTAAN:
                _tolak(q, JENDELA_DETIK, sekarang, "Terlalu banyak permintaan. Coba lagi dalam {menit} menit.")
            antrian.append(q)
        for q in antrian:
            q.append(sekarang)


def cek_login_diblokir(email: str, ip: str) -> None:
    """Tolak login (bahkan dengan password benar) bila email/IP sudah terlalu sering gagal."""
    sekarang = time.monotonic()
    pesan = "Terlalu banyak percobaan login gagal. Coba lagi dalam {menit} menit atau gunakan Lupa Kata Sandi."
    with _lock:
        for kunci, batas in ((f"login:email:{email}", BATAS_LOGIN_GAGAL_EMAIL),
                             (f"login:ip:{ip}", BATAS_LOGIN_GAGAL_IP)):
            q = _antrian(kunci, JENDELA_LOGIN_DETIK, sekarang)
            if len(q) >= batas:
                _tolak(q, JENDELA_LOGIN_DETIK, sekarang, pesan)


def catat_login_gagal(email: str, ip: str) -> None:
    sekarang = time.monotonic()
    with _lock:
        _riwayat[f"login:email:{email}"].append(sekarang)
        _riwayat[f"login:ip:{ip}"].append(sekarang)


def reset_login_gagal(email: str) -> None:
    """Login berhasil: hapus hitungan gagal untuk email ini (hitungan IP tetap)."""
    with _lock:
        _riwayat.pop(f"login:email:{email}", None)
