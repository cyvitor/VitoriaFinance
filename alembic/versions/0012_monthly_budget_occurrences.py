"""Registra a liberação do saldo de orçamento em uma competência."""
from alembic import op
import sqlalchemy as sa

revision = "0012_budget_occurrences"
down_revision = "0011_budget_projection"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "monthly_budget_occurrences" not in inspector.get_table_names():
        op.create_table(
            "monthly_budget_occurrences",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("monthly_budget_id", sa.Integer(), nullable=False),
            sa.Column("year", sa.Integer(), nullable=False),
            sa.Column("month", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(30), nullable=False, server_default="released"),
            sa.Column("released_amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
            sa.Column("released_at", sa.DateTime(), nullable=True),
            sa.Column("released_by_id", sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(["monthly_budget_id"], ["monthly_budgets.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["released_by_id"], ["users.id"], ondelete="SET NULL"),
            sa.UniqueConstraint("monthly_budget_id", "year", "month"),
        )
        op.create_index("ix_budget_occurrence_budget", "monthly_budget_occurrences", ["monthly_budget_id"])
        op.create_index("ix_budget_occurrence_year", "monthly_budget_occurrences", ["year"])
        op.create_index("ix_budget_occurrence_month", "monthly_budget_occurrences", ["month"])


def downgrade():
    if "monthly_budget_occurrences" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("monthly_budget_occurrences")
