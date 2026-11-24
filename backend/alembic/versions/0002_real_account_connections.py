"""Add separate owner settings for real-account balance reads."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.create_table(
        "real_account_connections",
        sa.Column("exchange", sa.String(length=10), primary_key=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("ciphertext", sa.Text()),
        sa.Column("last_sync", sa.DateTime(timezone=True)),
        sa.Column("currency", sa.String(length=20)),
        sa.Column("balance", sa.Numeric(32, 16)),
        sa.CheckConstraint("exchange IN ('DERIV','KALSHI')", name="real_account_exchange"),
    )
    op.bulk_insert(connection, [
        {"exchange": exchange, "status": "NOT_CONFIGURED", "enabled": False}
        for exchange in ("DERIV", "KALSHI")
    ])


def downgrade() -> None:
    op.drop_table("real_account_connections")
