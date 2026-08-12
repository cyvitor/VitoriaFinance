"""Adiciona orçamentos mensais por categoria."""
from alembic import op

from app.database import Base
from app import models  # noqa: F401

revision = "0010_monthly_budgets"
down_revision = "0009_card_billing_periods"
branch_labels = None
depends_on = None


def upgrade():
    Base.metadata.tables["monthly_budgets"].create(bind=op.get_bind(), checkfirst=True)


def downgrade():
    Base.metadata.tables["monthly_budgets"].drop(bind=op.get_bind(), checkfirst=True)
