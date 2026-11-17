"""Password hashing, session tokens and verification codes."""
import base64
import hashlib
import re
import secrets
import uuid
from datetime import timedelta

import bcrypt
import jwt

from .config import get_settings
from .database import utcnow

ALGORITHM = "HS256"
# Absolute lifetime of a token. Idle expiry (8 h) is enforced on the session row.
TOKEN_MAX_DAYS = 30


def _prehash(password: str) -> bytes:
    # bcrypt reads at most 72 bytes; pre-hashing keeps long passphrases distinct.
    return base64.b64encode(hashlib.sha256(password.encode("utf-8")).digest())


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt(rounds=get_settings().bcrypt_rounds)).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_prehash(password), password_hash.encode("ascii"))
    except ValueError:
        return False


def password_problems(password: str) -> list[str]:
    """FR-01 complexity rule; returns the unmet conditions."""
    problems = []
    if len(password) < 8:
        problems.append("at least 8 characters")
    if not re.search(r"[A-Z]", password):
        problems.append("an uppercase letter")
    if not re.search(r"\d", password):
        problems.append("a number")
    if not re.search(r"[^A-Za-z0-9]", password):
        problems.append("a special character")
    return problems


def new_session_id() -> str:
    return str(uuid.uuid4())


def create_token(user_id: int, session_id: str) -> str:
    now = utcnow()
    payload = {"sub": str(user_id), "jti": session_id, "iat": now, "exp": now + timedelta(days=TOKEN_MAX_DAYS)}
    return jwt.encode(payload, get_settings().secret_key, algorithm=ALGORITHM)


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, get_settings().secret_key, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None


def new_verification_code() -> str:
    return "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))


def hash_code(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode("utf-8")).hexdigest()
