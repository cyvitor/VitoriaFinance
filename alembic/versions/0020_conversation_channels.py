"""Separate app chat from Telegram display while retaining shared AI context."""
from alembic import op
import sqlalchemy as sa

revision = "0020_conversation_channels"
down_revision = "0019_mobile_api_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("telegram_conversation_messages", sa.Column(
        "channel", sa.String(length=20), nullable=False, server_default="telegram"))
    op.create_index("ix_telegram_conversation_messages_channel",
                    "telegram_conversation_messages", ["channel"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_telegram_conversation_messages_channel", table_name="telegram_conversation_messages")
    op.drop_column("telegram_conversation_messages", "channel")
