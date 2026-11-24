"""Compare a frozen, disposable source with its independently restored database."""
import os
import subprocess

from sqlalchemy import MetaData, create_engine, select, text
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


# Compare server-generated definitions, not Python object repr or internal OIDs.
# Every query is restricted to the public schema of the disposable Compose DB.
CATALOGS = {
    "tables": """
        SELECT c.relname, c.relkind, c.relpersistence, c.relrowsecurity,
               c.relforcerowsecurity, c.reloptions
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
        ORDER BY c.relname
    """,
    "columns": """
        SELECT c.relname, a.attname,
               row_number() OVER (PARTITION BY c.relname ORDER BY a.attnum),
               pg_catalog.format_type(a.atttypid, a.atttypmod),
               a.attnotnull, a.attidentity, a.attgenerated,
               pg_catalog.pg_get_expr(d.adbin, d.adrelid, false),
               cn.nspname, co.collname, a.attstorage, a.attcompression
        FROM pg_catalog.pg_attribute a
        JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
        LEFT JOIN pg_catalog.pg_collation co ON co.oid = a.attcollation
        LEFT JOIN pg_catalog.pg_namespace cn ON cn.oid = co.collnamespace
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
              AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY c.relname, a.attnum
    """,
    "constraints": """
        SELECT c.relname, k.conname, k.contype,
               pg_catalog.pg_get_constraintdef(k.oid, false),
               k.convalidated, k.condeferrable, k.condeferred
        FROM pg_catalog.pg_constraint k
        JOIN pg_catalog.pg_class c ON c.oid = k.conrelid
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
        ORDER BY c.relname, k.conname
    """,
    "indexes": """
        SELECT c.relname, i.relname, pg_catalog.pg_get_indexdef(i.oid),
               x.indisunique, x.indisprimary, x.indisvalid, x.indisready,
               i.reloptions
        FROM pg_catalog.pg_index x
        JOIN pg_catalog.pg_class c ON c.oid = x.indrelid
        JOIN pg_catalog.pg_class i ON i.oid = x.indexrelid
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
        ORDER BY c.relname, i.relname
    """,
    "sequences": """
        SELECT sequencename, data_type::text, start_value, min_value,
               max_value, increment_by, cycle, cache_size, last_value
        FROM pg_catalog.pg_sequences WHERE schemaname = 'public'
        ORDER BY sequencename
    """,
    "sequence_ownership": """
        SELECT s.relname, c.relname, a.attname, d.deptype
        FROM pg_catalog.pg_depend d
        JOIN pg_catalog.pg_class s ON s.oid = d.objid AND s.relkind = 'S'
        JOIN pg_catalog.pg_namespace n ON n.oid = s.relnamespace
        JOIN pg_catalog.pg_class c ON c.oid = d.refobjid
        JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid AND a.attnum = d.refobjsubid
        WHERE n.nspname = 'public' AND d.classid = 'pg_class'::regclass
              AND d.refclassid = 'pg_class'::regclass AND d.deptype IN ('a', 'i')
        ORDER BY s.relname, c.relname, a.attname
    """,
}


def schema_signature(connection):
    result = {
        name: [tuple(row) for row in connection.execute(text(query))]
        for name, query in CATALOGS.items()
    }
    # last_value alone does not distinguish a never-called sequence from setval.
    preparer = connection.dialect.identifier_preparer
    result["sequence_state"] = [
        (row[0], *connection.execute(text(
            "SELECT last_value, is_called FROM public." + preparer.quote_identifier(row[0])
        )).one())
        for row in result["sequences"]
    ]
    return result


def compare_schema(original, recovered):
    before, after = schema_signature(original), schema_signature(recovered)
    for category in before:
        if before[category] != after[category]:
            # Catalog object names only, not data, expressions or connection values.
            original_rows = before[category]
            restored_rows = after[category]
            missing = [row[0] for row in original_rows if row not in restored_rows]
            extra = [row[0] for row in restored_rows if row not in original_rows]
            raise SystemExit(
                f"RESTORE_SCHEMA_MISMATCH category={category} "
                f"source_objects={sorted(set(missing))} restored_objects={sorted(set(extra))}"
            )
    print("RESTORE_SCHEMA_CATALOG_OK categories=" + str(len(before)))


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
            compare_schema(original, recovered)
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
