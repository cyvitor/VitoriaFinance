"""Add SMART financial goals for app conversations."""
from alembic import op
import sqlalchemy as sa

revision = "0021_smart_goals"
down_revision = "0020_conversation_channels"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "financial_goals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("person_id", sa.Integer(), sa.ForeignKey("people.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("goal_type", sa.String(20), nullable=False), sa.Column("title", sa.String(180), nullable=False),
        sa.Column("sentence", sa.Text(), nullable=False), sa.Column("target_amount", sa.Numeric(14, 2)),
        sa.Column("saved_amount", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column("weekly_amount", sa.Numeric(14, 2)), sa.Column("days_per_week", sa.Integer()),
        sa.Column("hour_label", sa.String(8)), sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False), sa.Column("weeks", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    for col in ("workspace_id", "person_id", "user_id", "status"):
        op.create_index(f"ix_financial_goals_{col}", "financial_goals", [col])


def downgrade() -> None:
    op.drop_table("financial_goals")
