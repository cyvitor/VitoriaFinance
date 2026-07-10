"""Adiciona rascunhos e idempotencia para gastos pelo Telegram."""
from alembic import op
import sqlalchemy as sa

from app.database import Base
from app import models  # noqa: F401

revision = "0004_telegram_expenses"
down_revision = "0003_telegram_auth"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    op.add_column("transactions", sa.Column("source", sa.String(30), nullable=False, server_default="web"))
    op.create_index("ix_transactions_source", "transactions", ["source"])
    Base.metadata.tables["telegram_expense_drafts"].create(bind=bind, checkfirst=True)
    Base.metadata.tables["telegram_processed_updates"].create(bind=bind, checkfirst=True)


def downgrade():
    bind = op.get_bind()
    Base.metadata.tables["telegram_processed_updates"].drop(bind=bind, checkfirst=True)
    Base.metadata.tables["telegram_expense_drafts"].drop(bind=bind, checkfirst=True)
    op.drop_index("ix_transactions_source", table_name="transactions")
    op.drop_column("transactions", "source")
