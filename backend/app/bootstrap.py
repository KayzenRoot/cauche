"""Create the initial owner using a local password prompt or mounted secret."""

import argparse
import os
import sys
from getpass import getpass
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

from .db import SessionLocal, lock
from .events import audit
from .models import User
from .schemas import normalize_email
from .security import (
    AUTH_LOCK,
    PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
    hash_password,
    validate_password_length,
)

PASSWORD_REQUIREMENT = f"A senha deve ter de {PASSWORD_MIN_LENGTH} a {PASSWORD_MAX_LENGTH} caracteres."


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


def prompt_password() -> str:
    print(PASSWORD_REQUIREMENT, file=sys.stderr)
    while True:
        password = getpass("Senha do proprietário: ")
        try:
            validate_password_length(password)
        except ValueError:
            print(PASSWORD_REQUIREMENT, file=sys.stderr)
            continue
        if getpass("Repita a senha: ") == password:
            return password
        print("As senhas não coincidem. Tente novamente.", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Criar o primeiro proprietário")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password-file", type=Path)
    options = parser.parse_args()
    try:
        email = normalize_email(options.email)
    except ValueError:
        parser.exit(2, "Informe um endereço de e-mail válido.\n")
    try:
        if options.password_file:
            password = options.password_file.read_text(encoding="utf-8").rstrip("\r\n")
        elif "CAUCHE_BOOTSTRAP_PASSWORD" in os.environ:
            password = os.environ["CAUCHE_BOOTSTRAP_PASSWORD"]
        else:
            password = prompt_password()
        validate_password_length(password)
    except (OSError, UnicodeError):
        parser.exit(2, "Não foi possível ler a senha. Verifique a fonte informada.\n")
    except ValueError:
        parser.exit(2, PASSWORD_REQUIREMENT + "\n")
    except (EOFError, KeyboardInterrupt):
        parser.exit(2, "\nCadastro cancelado.\n")
    try:
        created = bootstrap(email, password)
    except SQLAlchemyError:
        parser.exit(2, "Não foi possível gravar o proprietário. Verifique o banco e a migração.\n")
    print("Proprietário criado." if created else "O proprietário já está cadastrado.")


if __name__ == "__main__":
    main()
