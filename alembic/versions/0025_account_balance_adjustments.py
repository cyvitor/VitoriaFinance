"""Auditoria do ajuste de saldo pelo extrato.

Não cria receita. Instalações novas já recebem a tabela em 0001.
"""
from alembic import op
import sqlalchemy as sa

revision = "0025_account_balance_adjustments"
down_revision = "0024_async_audio_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "account_balance_adjustments" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "account_balance_adjustments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("adjustment_date", sa.Date(), nullable=False),
        sa.Column("previous_balance", sa.Numeric(14, 2), nullable=False),
        sa.Column("new_balance", sa.Numeric(14, 2), nullable=False),
        sa.Column("previous_initial_balance", sa.Numeric(14, 2), nullable=False),
        sa.Column("new_initial_balance", sa.Numeric(14, 2), nullable=False),
        sa.Column("description", sa.String(180), nullable=False),
        sa.Column("notes", sa.String(180), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_account_balance_adjustments_account_id", "account_balance_adjustments", ["account_id"])
    op.create_index("ix_account_balance_adjustments_user_id", "account_balance_adjustments", ["user_id"])
    op.create_index("ix_account_balance_adjustments_adjustment_date", "account_balance_adjustments", ["adjustment_date"])


def downgrade() -> None:
    if "account_balance_adjustments" not in sa.inspect(op.get_bind()).get_table_names():
        return
    # Os indices saem junto com a tabela (alguns sustentam as chaves estrangeiras no MySQL/MariaDB).
    op.drop_table("account_balance_adjustments")
