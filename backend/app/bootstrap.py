"""Create the initial owner using a local password prompt or mounted secret."""

import argparse
import os
from getpass import getpass
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from .db import SessionLocal, lock
from .events import audit
from .models import User
from .schemas import normalize_email
from .security import AUTH_LOCK, hash_password


def bootstrap(email: str, password: str) -> bool:
    email = normalize_email(email)
    encoded = hash_password(password)
    with SessionLocal.begin() as session:
        lock(session, AUTH_LOCK)
        if session.get(User, 1) is not None:
            return False
        session.add(User(id=1, email=email, password_hash=encoded, role="admin"))
        session.flush()
        audit(session, "AUTH", "OWNER_BOOTSTRAPPED", "Conta do proprietário criada.", user_id=1)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap the singleton administrator")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password-file", type=Path)
    options = parser.parse_args()
    try:
        if options.password_file:
            password = options.password_file.read_text(encoding="utf-8").rstrip("\r\n")
        elif "CAUCHE_BOOTSTRAP_PASSWORD" in os.environ:
            password = os.environ["CAUCHE_BOOTSTRAP_PASSWORD"]
        else:
            password = getpass("Owner password: ")
            if getpass("Repeat password: ") != password:
                parser.exit(2, "Passwords differ.\n")
        created = bootstrap(options.email, password)
    except (OSError, ValueError, SQLAlchemyError):
        parser.exit(2, "Bootstrap failed. Verify migrations, email, and a 14–128 character password.\n")
    print("Owner created." if created else "Owner already exists.")


if __name__ == "__main__":
    main()
