"""Manage the disposable PostgreSQL instance used by the backend CI job.

The runtime secret is generated per run, masked in the workflow log, written to
protected temporary files, and removed after the job. Only non-secret paths and
the published loopback port are exported to later steps.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

IMAGE = "postgres:17-alpine@sha256:526f86d93c6ea08fd3f1fe5605650cacc781b2c6013169a0ad8f42c7d3d84f32"
CONTAINER = "cauche-ci-postgres"
USER = "cauche_test"
DATABASE = "cauche_test"


def run(args, *, check=True):
    return subprocess.run(args, check=check, text=True, capture_output=True)


def write_private(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def fail(message: str) -> None:
    print(f"CI_POSTGRES_ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def start() -> None:
    temp = Path(os.environ["RUNNER_TEMP"])
    password = secrets.token_hex(24)
    print(f"::add-mask::{password}", flush=True)

    password_file = temp / "cauche-ci-postgres-password"
    server_env = temp / "cauche-ci-postgres.env"
    client_env = temp / "cauche-ci-postgres-client.env"
    empty_env = temp / "cauche-ci-postgres-empty.env"
    wrong_env = temp / "cauche-ci-postgres-wrong.env"
    state_file = temp / "cauche-ci-postgres.json"

    write_private(password_file, password)
    write_private(server_env, f"POSTGRES_PASSWORD={password}\n")
    write_private(client_env, f"PGPASSWORD={password}\nPGSSLMODE=disable\n")
    write_private(empty_env, "PGPASSWORD=\nPGSSLMODE=disable\n")
    write_private(wrong_env, f"PGPASSWORD={secrets.token_hex(16)}-wrong\nPGSSLMODE=disable\n")

    run(["docker", "rm", "-f", "-v", CONTAINER], check=False)
    run(
        [
            "docker", "run", "-d", "--name", CONTAINER,
            "--env-file", str(server_env),
            "-e", f"POSTGRES_USER={USER}",
            "-e", f"POSTGRES_DB={DATABASE}",
            "-e", "POSTGRES_HOST_AUTH_METHOD=scram-sha-256",
            "-e", "POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256 --auth-local=trust",
            "-p", "127.0.0.1::5432",
            "--shm-size=256m",
            "--health-cmd", f"pg_isready -U {USER} -d {DATABASE}",
            "--health-interval", "2s",
            "--health-timeout", "2s",
            "--health-retries", "30",
            IMAGE,
        ]
    )

    published = run(["docker", "port", CONTAINER, "5432/tcp"]).stdout.splitlines()
    if not published or not published[0].startswith("127.0.0.1:"):
        fail("published port is not loopback-only")
    try:
        port = int(published[0].rsplit(":", 1)[1])
    except (IndexError, ValueError):
        fail("published loopback port is invalid")
    print(f"POSTGRES_LOOPBACK_BINDING_OK 127.0.0.1:{port}")

    deadline = time.monotonic() + 120
    status = ""
    while time.monotonic() < deadline:
        probe = run(["docker", "inspect", "-f", "{{.State.Health.Status}}", CONTAINER], check=False)
        status = probe.stdout.strip()
        if status == "healthy":
            break
        time.sleep(2)
    if status != "healthy":
        logs = run(["docker", "logs", CONTAINER], check=False)
        print(logs.stdout)
        print(logs.stderr, file=sys.stderr)
        fail("database did not become healthy")

    rules = run(
        ["docker", "exec", CONTAINER, "sh", "-c",
         "grep -E '^host' /var/lib/postgresql/data/pg_hba.conf"]
    ).stdout.splitlines()
    print("PG_HBA_HOST_RULES:")
    for line in rules:
        print(line)
    if not rules or any("scram-sha-256" not in line for line in rules):
        fail("host authentication is not scram-sha-256")

    def psql(env_file: Path) -> subprocess.CompletedProcess[str]:
        return run(
            [
                "docker", "run", "--rm", "--network", "host",
                "--env-file", str(env_file), "--entrypoint", "psql", IMAGE,
                "-w", "-h", "127.0.0.1", "-p", str(port),
                "-U", USER, "-d", DATABASE, "-tAc",
                "select current_user, inet_server_port()",
            ],
            check=False,
        )

    positive = psql(client_env)
    if positive.returncode != 0 or positive.stdout.strip() != f"{USER}|5432":
        print(positive.stdout)
        print(positive.stderr, file=sys.stderr)
        fail("positive password authentication failed")
    print("POSTGRES_AUTH_POSITIVE_OK")

    def denied(env_file: Path, marker: str) -> None:
        result = psql(env_file)
        output = (result.stdout + result.stderr).lower()
        if result.returncode == 0:
            print(f"{marker}_UNEXPECTEDLY_ALLOWED")
            fail("passwordless authentication is enabled")
        if "password" not in output and "authentication" not in output:
            print(result.stdout)
            print(result.stderr, file=sys.stderr)
            fail("negative authentication test failed for a non-authentication reason")
        print(f"{marker}_DENIED")

    denied(empty_env, "POSTGRES_AUTH_NO_PASSWORD")
    denied(wrong_env, "POSTGRES_AUTH_WRONG_PASSWORD")

    for path in (client_env, empty_env, wrong_env):
        path.unlink(missing_ok=True)

    write_private(
        state_file,
        json.dumps(
            {
                "container": CONTAINER,
                "port": port,
                "password_file": str(password_file),
                "env_file": str(server_env),
            }
        ),
    )
    with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as env:
        env.write(f"CAUCHE_TEST_POSTGRES_PORT={port}\n")
        env.write(f"CAUCHE_TEST_POSTGRES_PASSWORD_FILE={password_file}\n")
        env.write(f"CAUCHE_TEST_POSTGRES_CONTAINER={CONTAINER}\n")
        env.write(f"CAUCHE_TEST_POSTGRES_ENV_FILE={server_env}\n")


def stop() -> None:
    temp = Path(os.environ["RUNNER_TEMP"])
    state_file = temp / "cauche-ci-postgres.json"
    container = CONTAINER
    password_file = temp / "cauche-ci-postgres-password"
    env_file = temp / "cauche-ci-postgres.env"
    state = {}
    if state_file.exists():
        try:
            loaded = json.loads(state_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state = loaded
        except (OSError, ValueError):
            state = {}
    container = state.get("container", container)
    password_file = Path(state.get("password_file", password_file))
    env_file = Path(state.get("env_file", env_file))
    run(["docker", "rm", "-f", "-v", container], check=False)
    for path in (
        password_file,
        env_file,
        state_file,
        temp / "cauche-ci-postgres-client.env",
        temp / "cauche-ci-postgres-empty.env",
        temp / "cauche-ci-postgres-wrong.env",
    ):
        path.unlink(missing_ok=True)
    print("POSTGRES_CLEANUP_OK")


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in {"start", "stop"}:
        print("usage: ci_postgres.py start|stop", file=sys.stderr)
        raise SystemExit(2)
    if sys.argv[1] == "start":
        start()
    else:
        stop()


if __name__ == "__main__":
    main()
