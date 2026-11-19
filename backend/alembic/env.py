"""Run reviewed Alembic revisions against the configured database."""

from alembic import context

from app import models  # noqa: F401
from app.config import DATABASE_URL
from app.db import Base, engine


def run_migrations() -> None:
    if context.is_offline_mode():
        context.configure(url=DATABASE_URL, literal_binds=True, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()
        return

    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


run_migrations()
