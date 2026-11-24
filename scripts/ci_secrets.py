"""Generate and clean ephemeral Compose secrets for CI.

Secret values live only in protected temporary files. Later steps receive
non-secret file paths through GITHUB_ENV; the values are never written there.
"""

from __future__ import annotations

import base64
import os
import secrets
import subprocess
import sys
from pathlib import Path


def write_private(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def compose() -> None:
    temp = Path(os.environ["RUNNER_TEMP"])
    master_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    database_password = secrets.token_hex(24)
    print(f"::add-mask::{master_key}", flush=True)
    print(f"::add-mask::{database_password}", flush=True)
    env_file = temp / "cauche-compose.env"
    write_private(
        env_file,
        f"CAUCHE_MASTER_KEY={master_key}\nPOSTGRES_PASSWORD={database_password}\n",
    )
    with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as env:
        env.write(f"CAUCHE_COMPOSE_ENV_FILE={env_file}\n")
    print("COMPOSE_EPHEMERAL_SECRETS_OK")


def bootstrap() -> None:
    temp = Path(os.environ["RUNNER_TEMP"])
    compose_env = os.environ["CAUCHE_COMPOSE_ENV_FILE"]
    owner_password = secrets.token_urlsafe(24)
    print(f"::add-mask::{owner_password}", flush=True)
    password_file = temp / "cauche-test-owner-password"
    write_private(password_file, owner_password)
    with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as env:
        env.write(f"CAUCHE_TEST_PASSWORD_FILE={password_file}\n")
    subprocess.run(
        [
            "docker", "compose", "--env-file", compose_env, "exec", "-T", "api",
            "python", "-m", "app.bootstrap", "--email", "owner@example.test",
            "--password-file", "/dev/stdin",
        ],
        input=owner_password + "\n",
        text=True,
        check=True,
    )
    print("OWNER_BOOTSTRAP_OK")


def cleanup() -> None:
    temp = Path(os.environ["RUNNER_TEMP"])
    for key, default in (
        ("CAUCHE_COMPOSE_ENV_FILE", temp / "cauche-compose.env"),
        ("CAUCHE_TEST_PASSWORD_FILE", temp / "cauche-test-owner-password"),
    ):
        Path(os.environ.get(key, default)).unlink(missing_ok=True)
    print("EPHEMERAL_SECRET_FILES_REMOVED")


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in {"compose", "bootstrap", "cleanup"}:
        print("usage: ci_secrets.py compose|bootstrap|cleanup", file=sys.stderr)
        raise SystemExit(2)
    {"compose": compose, "bootstrap": bootstrap, "cleanup": cleanup}[sys.argv[1]]()


if __name__ == "__main__":
    main()
