"""Managed production rejects unsafe database and owner selections locally."""

from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from app.managed_runtime import StartupError, managed_database_url, production_owner

DSN = "postgresql://cauche_runtime.project:unused@aws-0-sa-east-1.pooler.supabase.com:5432/postgres"


def test_managed_database_enforces_session_mode_and_tls():
    config = make_url(managed_database_url(DSN, Path("/tmp/ca.pem")))
    assert config.drivername == "postgresql+psycopg"
    assert config.query["sslmode"] == "verify-full"
    assert config.query["sslrootcert"] == str(Path("/tmp/ca.pem"))
    assert config.query["gssencmode"] == "disable"


@pytest.mark.parametrize("dsn", [
    DSN.replace(":5432/", ":6543/"),
    DSN.replace("cauche_runtime.project", "postgres.project"),
    DSN.replace("cauche_runtime.project", "cauche_runtime"),
    DSN + "?sslmode=disable",
    DSN + "?hostaddr=127.0.0.1",
    DSN.replace("aws-0-sa-east-1.pooler.supabase.com", "localhost"),
])
def test_managed_database_rejects_transaction_pool_admin_or_non_tls_variants(dsn):
    with pytest.raises(StartupError, match="MANAGED_DATABASE_REQUIRES_SUPABASE_SESSION_TLS"):
        managed_database_url(dsn, Path("/tmp/ca.pem"))


def test_production_owner_rejects_local_fixture_identity(monkeypatch):
    monkeypatch.setenv("CAUCHE_OWNER_EMAIL", "owner@example.test")
    with pytest.raises(StartupError, match="MANAGED_OWNER_INVALID"):
        production_owner()
