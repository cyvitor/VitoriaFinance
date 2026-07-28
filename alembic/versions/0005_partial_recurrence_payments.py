"""add remaining amount to recurrence occurrences"""
from alembic import op
import sqlalchemy as sa

revision = "0005_partial_recurrence_payments"
down_revision = "0004_telegram_expenses"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("recurrence_occurrences")}
    if "remaining_amount" not in columns:
        op.add_column("recurrence_occurrences", sa.Column("remaining_amount", sa.Numeric(14, 2), nullable=True))


def downgrade():
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("recurrence_occurrences")}
    if "remaining_amount" in columns:
        op.drop_column("recurrence_occurrences", "remaining_amount")
