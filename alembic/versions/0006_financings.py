"""add financing contracts"""
from alembic import op
import sqlalchemy as sa

revision = "0006_financings"
down_revision = "0005_partial_recurrence_payments"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("financings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("person_id", sa.Integer(), sa.ForeignKey("people.id", ondelete="CASCADE"), nullable=False),
        sa.Column("recurrence_rule_id", sa.Integer(), sa.ForeignKey("recurrence_rules.id", ondelete="SET NULL"), nullable=True, unique=True),
        sa.Column("description", sa.String(180), nullable=False),
        sa.Column("paid_installments", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_installments", sa.Integer(), nullable=False),
        sa.Column("installment_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("financed_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("outstanding_balance", sa.Numeric(14, 2), nullable=True),
        sa.Column("nominal_interest_rate", sa.Numeric(7, 4), nullable=True),
        sa.Column("institution", sa.String(120), nullable=True),
        sa.Column("due_day", sa.Integer(), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_financings_workspace_id", "financings", ["workspace_id"])
    op.create_index("ix_financings_person_id", "financings", ["person_id"])
    op.create_index("ix_financings_status", "financings", ["status"])


def downgrade():
    op.drop_table("financings")
