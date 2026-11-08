"""Async audio messages: processing status, idempotency key and played state."""
from alembic import op
import sqlalchemy as sa

revision = "0024_async_audio_messages"
down_revision = "0023_private_chat_media"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("telegram_conversation_messages",
        sa.Column("status", sa.String(12), nullable=False, server_default="done"))
    op.add_column("telegram_conversation_messages", sa.Column("client_key", sa.String(64), nullable=True))
    op.create_index("ix_telegram_conversation_messages_status", "telegram_conversation_messages", ["status"])
    op.create_unique_constraint("uq_conversation_user_client_key", "telegram_conversation_messages",
        ["user_id", "client_key"])
    op.add_column("chat_media", sa.Column("played_at", sa.DateTime(), nullable=True))
    # Replies that already exist were heard (or can be replayed) before this change: do not autoplay them again.
    op.execute("UPDATE chat_media SET played_at = created_at WHERE kind = 'audio_response'")


def downgrade() -> None:
    op.drop_column("chat_media", "played_at")
    op.drop_constraint("uq_conversation_user_client_key", "telegram_conversation_messages", type_="unique")
    op.drop_index("ix_telegram_conversation_messages_status", table_name="telegram_conversation_messages")
    op.drop_column("telegram_conversation_messages", "client_key")
    op.drop_column("telegram_conversation_messages", "status")
