"""dados de abastecimento informados junto com a despesa

Revision ID: 0018_draft_fuel_details
Revises: 0017_telegram_bill_reminders
"""
from alembic import op
import sqlalchemy as sa

revision = "0018_draft_fuel_details"
down_revision = "0017_telegram_bill_reminders"
branch_labels = None
depends_on = None


def _columns():
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns("telegram_expense_drafts")}


def upgrade():
    if "fuel_details" not in _columns():
        op.add_column("telegram_expense_drafts", sa.Column("fuel_details", sa.Text(), nullable=True))


def downgrade():
    if "fuel_details" in _columns():
        op.drop_column("telegram_expense_drafts", "fuel_details")
