"""aviso diario de contas fixas no Telegram

Revision ID: 0017_telegram_bill_reminders
Revises: 0016_area_access_level
"""
from alembic import op
import sqlalchemy as sa

revision = "0017_telegram_bill_reminders"
down_revision = "0016_area_access_level"
branch_labels = None
depends_on = None


def upgrade():
    if "telegram_bill_reminders" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "telegram_bill_reminders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("reminder_date", sa.Date(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "reminder_date"),
    )
    op.create_index("ix_telegram_bill_reminders_user_id", "telegram_bill_reminders", ["user_id"])


def downgrade():
    if "telegram_bill_reminders" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_index("ix_telegram_bill_reminders_user_id", table_name="telegram_bill_reminders")
        op.drop_table("telegram_bill_reminders")
