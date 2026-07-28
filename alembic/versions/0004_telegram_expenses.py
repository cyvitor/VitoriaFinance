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
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("transactions")}
    if "source" not in columns:
        op.add_column("transactions", sa.Column("source", sa.String(30), nullable=False, server_default="web"))

    inspector = sa.inspect(bind)
    indexes = {index["name"] for index in inspector.get_indexes("transactions")}
    if "ix_transactions_source" not in indexes:
        op.create_index("ix_transactions_source", "transactions", ["source"])

    Base.metadata.tables["telegram_expense_drafts"].create(bind=bind, checkfirst=True)
    Base.metadata.tables["telegram_processed_updates"].create(bind=bind, checkfirst=True)


def downgrade():
    bind = op.get_bind()
    Base.metadata.tables["telegram_processed_updates"].drop(bind=bind, checkfirst=True)
    Base.metadata.tables["telegram_expense_drafts"].drop(bind=bind, checkfirst=True)
    inspector = sa.inspect(bind)
    indexes = {index["name"] for index in inspector.get_indexes("transactions")}
    if "ix_transactions_source" in indexes:
        op.drop_index("ix_transactions_source", table_name="transactions")

    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("transactions")}
    if "source" in columns:
        op.drop_column("transactions", "source")
