"""Compare a frozen, disposable source with its independently restored database."""
import os
import subprocess

from sqlalchemy import MetaData, create_engine, inspect, select, text
from sqlalchemy.engine import make_url

from app.config import DATABASE_URL
from app.security import decrypt_credentials, verify_password


def require(condition, code):
    if not condition:
        raise SystemExit(code)


def snapshot(connection, metadata):
    return {
        table.name: sorted(repr(tuple(row)) for row in connection.execute(select(table)))
        for table in metadata.sorted_tables
    }


def constraints(connection, names):
    inspector = inspect(connection)
    result = {}
    for name in names:
        result[name] = {
            method: sorted(repr(item) for item in getattr(inspector, method)(name))
            for method in ("get_columns", "get_foreign_keys", "get_indexes",
                           "get_unique_constraints", "get_check_constraints")
        }
        result[name]["pk"] = inspector.get_pk_constraint(name)
    return result


def main():
    source = make_url(DATABASE_URL)
    require(
        source.host == "db" and source.database == "cauche"
        and source.username == "cauche" and source.port == 5432,
        "RESTORE_REHEARSAL_REQUIRES_DISPOSABLE_COMPOSE_DATABASE",
    )
    restored = source.set(database="cauche_restore_ci")
    options = {"hide_parameters": True, "connect_args": {"options": "-c timezone=UTC"}}
    original_engine = create_engine(source, **options)
    restored_engine = create_engine(restored, **options)
    try:
        with original_engine.connect() as original, restored_engine.connect() as recovered:
            original_meta, recovered_meta = MetaData(), MetaData()
            original_meta.reflect(bind=original)
            recovered_meta.reflect(bind=recovered)
            names = sorted(original_meta.tables)
            require(names == sorted(recovered_meta.tables), "RESTORE_TABLE_SET_MISMATCH")
            require(constraints(original, names) == constraints(recovered, names),
                    "RESTORE_SCHEMA_MISMATCH")
            rows = snapshot(original, original_meta)
            require(rows == snapshot(recovered, recovered_meta), "RESTORE_ROW_MISMATCH")
            owner = recovered.execute(text(
                "SELECT email, password_hash, role FROM users WHERE id = 1"
            )).one()
            require(owner.email == "owner@example.test" and owner.role == "admin"
                    and verify_password(owner.password_hash, os.environ["CAUCHE_TEST_PASSWORD"]),
                    "RESTORE_OWNER_AUTHENTICATION_FAILED")
            encrypted = recovered.execute(text(
                "SELECT ciphertext FROM real_account_connections WHERE exchange = 'DERIV'"
            )).scalar_one()
            require(bool(encrypted) and "synthetic-real-fixture" not in encrypted,
                    "RESTORE_CREDENTIAL_NOT_ENCRYPTED")
            require(decrypt_credentials(encrypted).get("token") == "synthetic-real-fixture",
                    "RESTORE_CREDENTIAL_DECRYPTION_FAILED")
            require(recovered.execute(text(
                "SELECT version_num FROM alembic_version"
            )).scalar_one() == "0002", "RESTORE_REVISION_MISMATCH")
            require(bool(rows["backtest_runs"]), "RESTORE_BACKTEST_EVIDENCE_MISSING")
            print(f"RESTORE_REHEARSAL_OK tables={len(names)} rows={sum(map(len, rows.values()))}")
            print("RESTORE_OWNER_AUTH_OK ENCRYPTED_CREDENTIAL_OK")
        env = {**os.environ, "DATABASE_URL": restored.render_as_string(hide_password=False)}
        subprocess.run(["alembic", "current", "--check-heads"], env=env, check=True)
        subprocess.run(["alembic", "check"], env=env, check=True)
    finally:
        original_engine.dispose()
        restored_engine.dispose()


if __name__ == "__main__":
    main()
