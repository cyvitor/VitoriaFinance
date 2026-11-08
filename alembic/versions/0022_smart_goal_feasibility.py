"""Store the user's choice to keep a goal above their recent surplus."""
from alembic import op
import sqlalchemy as sa

revision = "0022_smart_goal_feasibility"
down_revision = "0021_smart_goals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("financial_goals", sa.Column("is_tight", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("financial_goals", "is_tight")
