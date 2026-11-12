"""Anexa mídia privada (áudio e imagem) às mensagens do chat.

O arquivo fica fora da pasta pública. Instalações novas já criam a tabela em 0001.
"""
from alembic import op
import sqlalchemy as sa

revision = "0023_private_chat_media"
down_revision = "0020_conversation_channels"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "chat_media" in sa.inspect(op.get_bind()).get_table_names():
        return
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
        sa.Column("played_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_chat_media_user_id", "chat_media", ["user_id"])
    op.create_index("ix_chat_media_message_id", "chat_media", ["message_id"])
    op.create_index("ix_chat_media_kind", "chat_media", ["kind"])
    op.create_index("ix_chat_media_created_at", "chat_media", ["created_at"])


def downgrade() -> None:
    if "chat_media" not in sa.inspect(op.get_bind()).get_table_names():
        return
    op.drop_index("ix_chat_media_created_at", table_name="chat_media")
    op.drop_index("ix_chat_media_kind", table_name="chat_media")
    op.drop_index("ix_chat_media_message_id", table_name="chat_media")
    op.drop_index("ix_chat_media_user_id", table_name="chat_media")
    op.drop_table("chat_media")
