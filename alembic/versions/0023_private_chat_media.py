"""Add user-owned private media attachments to app chat."""
from alembic import op
import sqlalchemy as sa

revision = "0023_private_chat_media"
down_revision = "0022_smart_goal_feasibility"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_media",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("message_id", sa.Integer(), sa.ForeignKey("telegram_conversation_messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("storage_key", sa.String(80), nullable=False, unique=True),
        sa.Column("filename", sa.String(180), nullable=False),
        sa.Column("mime_type", sa.String(80), nullable=False),
        sa.Column("transcript", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_chat_media_user_id", "chat_media", ["user_id"])
    op.create_index("ix_chat_media_message_id", "chat_media", ["message_id"])
    op.create_index("ix_chat_media_kind", "chat_media", ["kind"])
    op.create_index("ix_chat_media_created_at", "chat_media", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_chat_media_created_at", table_name="chat_media")
    op.drop_index("ix_chat_media_kind", table_name="chat_media")
    op.drop_index("ix_chat_media_message_id", table_name="chat_media")
    op.drop_index("ix_chat_media_user_id", table_name="chat_media")
    op.drop_table("chat_media")
