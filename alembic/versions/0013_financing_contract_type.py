"""identify financing contracts and negotiated debts

Revision ID: 0013_financing_contract_type
Revises: 0012_monthly_budget_occurrences
"""
from alembic import op
import sqlalchemy as sa

revision = "0013_financing_contract_type"
down_revision = "0012_budget_occurrences"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("financings", sa.Column("contract_type", sa.String(length=30), nullable=False, server_default="financing"))


def downgrade():
    op.drop_column("financings", "contract_type")
