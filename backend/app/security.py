"""Owner sessions, request origin checks and encrypted demo credentials."""

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .config import APP_ORIGIN, COOKIE_NAME, SECURE_COOKIE, SESSION_HOURS
from .db import SessionLocal, aware, lock, utcnow
from .events import audit
from .models import LoginThrottle, User
from .models import Session as StoredSession

AUTH_LOCK = 18902462
HASHER = PasswordHasher()
DUMMY_HASH = HASHER.hash(secrets.token_urlsafe(32))


def hash_password(password: str) -> str:
    if not 14 <= len(password) <= 128:
        raise ValueError("PASSWORD_LENGTH")
    return HASHER.hash(password)


def verify_password(encoded: str, password: str) -> bool:
    try:
        return HASHER.verify(encoded, password)
    except (VerificationError, InvalidHashError):
        return False


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def csrf_for(token: str) -> str:
    return hmac.new(token.encode(), b"CAUCHE-CSRF-V1", hashlib.sha256).hexdigest()


def check_origin(origin: str | None) -> None:
    if origin != APP_ORIGIN:
        raise HTTPException(403, "ORIGIN_DENIED")


def _fernet() -> Fernet:
    try:
        return Fernet(os.getenv("CAUCHE_MASTER_KEY", "").encode())
    except (TypeError, ValueError):
        raise ValueError("MASTER_KEY_NOT_CONFIGURED") from None


def encrypt_credentials(credentials: dict[str, str]) -> str:
    return _fernet().encrypt(json.dumps(credentials, separators=(",", ":")).encode()).decode()


def decrypt_credentials(ciphertext: str) -> dict[str, str]:
    try:
        result = json.loads(_fernet().decrypt(ciphertext.encode()))
        if not isinstance(result, dict) or any(not isinstance(value, str) for value in result.values()):
            raise ValueError("CREDENTIALS_UNAVAILABLE")
        return result
    except (InvalidToken, UnicodeError, json.JSONDecodeError):
        raise ValueError("CREDENTIALS_UNAVAILABLE") from None


@dataclass(frozen=True)
class Identity:
    id: int
    email: str
    role: str
    token: str


def authenticate(session: Session, token: str | None) -> Identity | None:
    if not token or len(token) > 128:
        return None
    row = session.execute(
        select(StoredSession, User)
        .join(User, StoredSession.user_id == User.id)
        .where(StoredSession.token_hash == digest(token))
    ).first()
    if row is None:
        return None
    stored, user = row
    if aware(stored.expires_at) <= utcnow():
        return None
    return Identity(user.id, user.email, user.role, token)


def db_session():
    with SessionLocal() as session:
        yield session


def identity(request: Request) -> Identity:
    with SessionLocal() as session:
        owner = authenticate(session, request.cookies.get(COOKIE_NAME))
    if owner is None:
        raise HTTPException(401, "UNAUTHENTICATED")
    return owner


def check_csrf(request: Request, owner: Identity) -> None:
    check_origin(request.headers.get("origin"))
    supplied = request.headers.get("x-csrf-token", "")
    if not supplied.isascii() or not hmac.compare_digest(supplied, csrf_for(owner.token)):
        raise HTTPException(403, "CSRF_DENIED")


def require_admin(owner: Identity) -> None:
    if owner.role != "admin":
        raise HTTPException(403, "ADMIN_REQUIRED")


def login(session: Session, email: str, password: str, peer: str, response: Response) -> dict:
    lock(session, AUTH_LOCK)
    now = utcnow()
    throttle_key = digest(f"login-v1:{peer}")
    throttle = session.get(LoginThrottle, throttle_key)
    if throttle is None:
        throttle = LoginThrottle(key=throttle_key, attempts=0, window_start=now)
        session.add(throttle)
    if aware(throttle.window_start) <= now - timedelta(minutes=5):
        throttle.window_start, throttle.attempts = now, 0
    if throttle.attempts >= 8:
        raise HTTPException(429, "LOGIN_THROTTLED", headers={"Retry-After": "300"})

    owner = session.scalar(select(User).where(User.email == email))
    valid = verify_password(owner.password_hash if owner else DUMMY_HASH, password)
    if not valid or owner is None:
        throttle.attempts += 1
        audit(session, "AUTH", "LOGIN_FAILED", "Falha de autenticação.", severity="WARNING")
        session.commit()
        raise HTTPException(401, "INVALID_CREDENTIALS")

    throttle.attempts = 0
    session.execute(delete(StoredSession).where(StoredSession.expires_at <= now))
    if HASHER.check_needs_rehash(owner.password_hash):
        owner.password_hash = hash_password(password)
    token = secrets.token_urlsafe(32)
    session.add(StoredSession(token_hash=digest(token), user_id=owner.id,
                              expires_at=now + timedelta(hours=SESSION_HOURS)))
    audit(session, "AUTH", "LOGIN_SUCCEEDED", "Sessão iniciada.", user_id=owner.id)
    session.commit()
    response.set_cookie(COOKIE_NAME, token, max_age=SESSION_HOURS * 3600, path="/",
                        httponly=True, secure=SECURE_COOKIE, samesite="strict")
    return session_payload(Identity(owner.id, owner.email, owner.role, token))


def clear_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/", secure=SECURE_COOKIE,
                           httponly=True, samesite="strict")


def session_payload(owner: Identity) -> dict:
    return {"email": owner.email, "role": owner.role, "csrf_token": csrf_for(owner.token)}
