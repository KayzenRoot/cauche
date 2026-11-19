"""Start the privately hosted Cloudflare container against managed PostgreSQL."""

import os
import re
import signal
import ssl
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

MIGRATION_LOCK = 191008302
UI_DIR = Path("/app/ui")
WORKER_HOST = re.compile(r"cauche-pulse\.[a-z0-9-]+\.workers\.dev")
SUPABASE_HOST = re.compile(
    r"db\.[a-z0-9]+\.supabase\.co|aws-[0-9]+-[a-z0-9-]+\.pooler\.supabase\.com"
)
SUPABASE_APP_ROLE = re.compile(r"cauche_[a-z0-9_]+")


class StartupError(Exception):
    """A public-safe failure identifier for managed startup."""


def managed_database_url(raw_dsn: str, ca_file: Path) -> str:
    """Use only a dedicated-role, identity-verified, session-level connection."""
    try:
        url = make_url(raw_dsn)
    except (TypeError, ValueError, SQLAlchemyError):
        raise StartupError("MANAGED_DATABASE_URL_INVALID") from None
    role, separator, project_ref = (url.username or "").partition(".")
    session_pooler = (url.host or "").endswith(".pooler.supabase.com")
    valid_pooler_user = re.fullmatch(r"[a-z0-9]+", project_ref) if session_pooler else not separator
    if (
        url.drivername not in {"postgresql", "postgresql+psycopg"}
        or not SUPABASE_HOST.fullmatch(url.host or "")
        or url.port != 5432
        or not SUPABASE_APP_ROLE.fullmatch(role)
        or not valid_pooler_user
        or not url.password
        or not url.database
        or set(url.query) - {"sslmode"}
        or url.query.get("sslmode") not in {None, "verify-full"}
    ):
        raise StartupError("MANAGED_DATABASE_REQUIRES_SUPABASE_SESSION_TLS")
    return url.set(
        drivername="postgresql+psycopg",
        query={
            "sslmode": "verify-full", "sslrootcert": str(ca_file),
            "gssencmode": "disable", "connect_timeout": "8",
        },
    ).render_as_string(hide_password=False)


def production_owner() -> str:
    from .schemas import normalize_email

    try:
        email = normalize_email(os.environ.get("CAUCHE_OWNER_EMAIL", ""))
    except ValueError:
        raise StartupError("MANAGED_OWNER_INVALID") from None
    domain = email.rsplit("@", 1)[1]
    if domain in {"example.com", "example.net", "example.org"} or domain.endswith(
        (".test", ".invalid", ".localhost", ".example")
    ):
        raise StartupError("MANAGED_OWNER_INVALID")
    return email


def configure(ca_file: Path) -> None:
    raw_origin = os.environ.get("APP_ORIGIN", "")
    origin = urlsplit(raw_origin)
    if not WORKER_HOST.fullmatch(origin.hostname or "") or raw_origin != f"https://{origin.hostname}":
        raise StartupError("MANAGED_ORIGIN_INVALID")
    try:
        Fernet(os.environ.get("CAUCHE_MASTER_KEY", "").encode())
    except ValueError:
        raise StartupError("MANAGED_MASTER_KEY_INVALID") from None

    pem = os.environ.pop("CAUCHE_DB_CA_PEM", "")
    if "-----BEGIN CERTIFICATE-----" not in pem:
        raise StartupError("MANAGED_DATABASE_CA_INVALID")
    try:
        ssl.create_default_context(cadata=pem)
    except (ValueError, ssl.SSLError):
        raise StartupError("MANAGED_DATABASE_CA_INVALID") from None
    ca_file.write_text(pem, encoding="ascii")
    os.environ["DATABASE_URL"] = managed_database_url(os.environ.get("DATABASE_URL", ""), ca_file)


def initialize_database(email: str, bootstrap_password: str | None) -> None:
    from .bootstrap import bootstrap
    from .db import engine
    from .models import User

    try:
        with engine.connect() as connection:
            if connection.scalar(text("SELECT current_schema()")) != "cauche":
                raise StartupError("MANAGED_DATABASE_SCHEMA_REQUIRED")
            if not connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": MIGRATION_LOCK}):
                raise StartupError("MANAGED_DATABASE_STARTUP_LOCK_BUSY")
            try:
                subprocess.run(
                    ["alembic", "upgrade", "head"], check=True, timeout=55,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                owner = connection.execute(select(User.email, User.role).where(User.id == 1)).one_or_none()
                if owner is None:
                    if not bootstrap_password:
                        raise StartupError("MANAGED_OWNER_BOOTSTRAP_REQUIRED")
                    bootstrap(email, bootstrap_password)
                    owner = connection.execute(select(User.email, User.role).where(User.id == 1)).one_or_none()
                if owner is None or owner.email.lower() != email or owner.role != "admin":
                    raise StartupError("MANAGED_OWNER_MISMATCH")
            finally:
                if not connection.invalidated:
                    connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK})
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise StartupError("MANAGED_MIGRATION_FAILED") from None
    except SQLAlchemyError:
        raise StartupError("MANAGED_DATABASE_UNAVAILABLE") from None
    except ValueError:
        raise StartupError("MANAGED_BOOTSTRAP_PASSWORD_INVALID") from None
    finally:
        engine.dispose()


def http_application():
    """Keep the SPA and API behind the exact same Cloudflare Access origin."""
    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    from .main import app

    @app.get("/{path:path}", include_in_schema=False)
    def ui(path: str):
        asset = (UI_DIR / path).resolve()
        if path == "api" or path.startswith("api/") or not asset.is_relative_to(UI_DIR):
            raise HTTPException(404, "NOT_FOUND")
        if asset.is_file():
            return FileResponse(asset)
        if path.startswith("assets/"):
            raise HTTPException(404, "NOT_FOUND")
        return FileResponse(UI_DIR / "index.html")

    return app


def supervise() -> int:
    stopped = Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopped.set())

    processes = []
    try:
        processes.append(subprocess.Popen([sys.executable, "-m", "app.worker"]))
        processes.append(subprocess.Popen([
            sys.executable, "-m", "uvicorn", "app.managed_runtime:http_application",
            "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log",
        ]))
        while not stopped.wait(0.25):
            if any(child.poll() is not None for child in processes):
                print("MANAGED_PROCESS_EXITED", file=sys.stderr)
                return 1
        return 0
    finally:
        for child in processes:
            if child.poll() is None:
                child.terminate()
        for child in processes:
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


def main() -> int:
    try:
        with TemporaryDirectory(prefix="cauche-tls-") as temporary:
            bootstrap_password = os.environ.pop("CAUCHE_BOOTSTRAP_PASSWORD", None)
            configure(Path(temporary) / "provider-ca.pem")
            initialize_database(production_owner(), bootstrap_password)
            del bootstrap_password
            return supervise()
    except StartupError as failure:
        print(str(failure), file=sys.stderr)
    except (OSError, ValueError):
        print("MANAGED_STARTUP_FAILED", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
