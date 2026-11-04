import os
import bcrypt
from datetime import datetime, timedelta, timezone
from jose import jwt, JWTError

SECRET_KEY = os.getenv("JWT_SECRET", "")
if not SECRET_KEY:
    # Railway selalu men-set RAILWAY_ENVIRONMENT_NAME. Di produksi, secret default = siapa pun
    # bisa memalsukan token login, jadi server menolak jalan.
    if os.getenv("RAILWAY_ENVIRONMENT_NAME"):
        raise RuntimeError("JWT_SECRET wajib di-set di Railway Variables.")
    SECRET_KEY = "kencana-secret-key-khusus-pengembangan-lokal"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 8  # 8 jam


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


def create_access_token(data: dict, expires_minutes: int = ACCESS_TOKEN_EXPIRE_MINUTES) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=expires_minutes)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None
