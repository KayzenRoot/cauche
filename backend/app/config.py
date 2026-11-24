"""Process settings for the API and worker."""

import os
from urllib.parse import urlsplit

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql+psycopg://cauche:cauche@localhost:5432/cauche"
)
APP_ORIGIN = os.getenv("APP_ORIGIN", "http://localhost:8080").rstrip("/")
_origin = urlsplit(APP_ORIGIN)
if (
    _origin.scheme not in {"http", "https"}
    or not _origin.hostname
    or _origin.username is not None
    or _origin.password is not None
    or _origin.path
    or _origin.query
    or _origin.fragment
):
    raise ValueError("APP_ORIGIN must be an HTTP(S) origin without a path")
if _origin.scheme == "http" and _origin.hostname not in {"localhost", "127.0.0.1", "::1"}:
    raise ValueError("Remote access requires an HTTPS APP_ORIGIN")

SECURE_COOKIE = _origin.scheme == "https"
COOKIE_NAME = "__Host-cauche" if SECURE_COOKIE else "cauche_local"
SESSION_HOURS = 12
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "30"))
if not 1 <= LOG_RETENTION_DAYS <= 365:
    raise ValueError("LOG_RETENTION_DAYS must be between 1 and 365")
