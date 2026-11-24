"""Verify that owner setup can recover from invalid input without exposing it."""

import sys

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app import bootstrap


def test_prompt_retries_short_password_and_mismatched_confirmation(monkeypatch, capsys):
    valid = "a" * 14
    answers = iter(("abcdef", "b" * 14, "c" * 14, valid, valid))
    prompts = []
    def fake_getpass(label):
        prompts.append(label)
        return next(answers)

    monkeypatch.setattr(bootstrap, "getpass", fake_getpass)

    assert bootstrap.prompt_password() == valid
    assert len(prompts) == 5
    error = capsys.readouterr().err
    assert error.count(bootstrap.PASSWORD_REQUIREMENT) == 2
    assert "As senhas não coincidem" in error
    assert valid not in error and "abcdef" not in error


def test_invalid_email_exits_before_asking_for_password(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["bootstrap", "--email", "invalid"])
    monkeypatch.setattr(bootstrap, "getpass", lambda _: pytest.fail("Password prompt opened"))

    with pytest.raises(SystemExit) as raised:
        bootstrap.main()
    assert raised.value.code == 2
    assert capsys.readouterr().err.strip() == "Informe um endereço de e-mail válido."


def test_noninteractive_short_password_reports_policy_without_writing(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["bootstrap", "--email", "owner@example.test"])
    monkeypatch.setenv("CAUCHE_BOOTSTRAP_PASSWORD", "abcdef")
    monkeypatch.setattr(bootstrap, "bootstrap", lambda *_: pytest.fail("Owner write called"))

    with pytest.raises(SystemExit) as raised:
        bootstrap.main()
    assert raised.value.code == 2
    assert capsys.readouterr().err.strip() == bootstrap.PASSWORD_REQUIREMENT


def test_database_failure_is_reported_without_exception_details(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["bootstrap", "--email", "owner@example.test"])
    monkeypatch.setenv("CAUCHE_BOOTSTRAP_PASSWORD", "a" * 14)

    def fail_to_write(*_):
        raise SQLAlchemyError("internal debug detail")

    monkeypatch.setattr(bootstrap, "bootstrap", fail_to_write)
    with pytest.raises(SystemExit) as raised:
        bootstrap.main()
    assert raised.value.code == 2
    error = capsys.readouterr().err
    assert "Verifique o banco e a migração" in error
    assert "internal debug detail" not in error
