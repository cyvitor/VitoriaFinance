"""Permite incluir o saldo restante do orçamento na projeção mensal."""
from alembic import op
import sqlalchemy as sa

revision = "0011_budget_projection"
down_revision = "0010_monthly_budgets"
branch_labels = None
depends_on = None


def upgrade():
    if "include_in_projection" not in {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("monthly_budgets")
    }:
        op.add_column("monthly_budgets", sa.Column(
            "include_in_projection", sa.Boolean(), nullable=False, server_default=sa.true()
        ))


def downgrade():
    if "include_in_projection" in {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("monthly_budgets")
    }:
        op.drop_column("monthly_budgets", "include_in_projection")
