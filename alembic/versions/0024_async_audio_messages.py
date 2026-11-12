"""Áudio assíncrono: status da mensagem, chave de idempotência e áudio já reproduzido.

Instalações novas já nascem com estas colunas (0001 + modelo atual). A restrição
única ignora client_key nulo, então as mensagens antigas do Telegram continuam válidas.
"""
from alembic import op
import sqlalchemy as sa

revision = "0024_async_audio_messages"
down_revision = "0023_private_chat_media"
branch_labels = None
depends_on = None


def _message_columns():
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns("telegram_conversation_messages")}


def _media_columns():
    bind = op.get_bind()
    if "chat_media" not in sa.inspect(bind).get_table_names():
        return set()
    return {item["name"] for item in sa.inspect(bind).get_columns("chat_media")}


def _has_client_key_constraint() -> bool:
    inspector = sa.inspect(op.get_bind())
    named = {item.get("name") for item in inspector.get_unique_constraints("telegram_conversation_messages")}
    if "uq_conversation_user_client_key" in named:
        return True
    for item in inspector.get_indexes("telegram_conversation_messages"):
        if item.get("unique") and set(item.get("column_names") or []) == {"user_id", "client_key"}:
            return True
    return False


def upgrade() -> None:
    if "status" not in _message_columns():
        op.add_column("telegram_conversation_messages",
            sa.Column("status", sa.String(12), nullable=False, server_default="done"))
    if "client_key" not in _message_columns():
        op.add_column("telegram_conversation_messages", sa.Column("client_key", sa.String(64), nullable=True))
    indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("telegram_conversation_messages")}
    if "ix_telegram_conversation_messages_status" not in indexes:
        op.create_index("ix_telegram_conversation_messages_status", "telegram_conversation_messages", ["status"])
    if not _has_client_key_constraint():
        with op.batch_alter_table("telegram_conversation_messages") as batch:
            batch.create_unique_constraint("uq_conversation_user_client_key", ["user_id", "client_key"])
    # played_at já entra na criação da tabela (0023 ou 0001). Só preenche respostas antigas.
    if "played_at" in _media_columns():
        op.execute("UPDATE chat_media SET played_at = created_at WHERE kind = 'audio_response' AND played_at IS NULL")


def downgrade() -> None:
    if "played_at" in _media_columns():
        op.drop_column("chat_media", "played_at")
    if _has_client_key_constraint():
        with op.batch_alter_table("telegram_conversation_messages") as batch:
            batch.drop_constraint("uq_conversation_user_client_key", type_="unique")
    indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("telegram_conversation_messages")}
    if "ix_telegram_conversation_messages_status" in indexes:
        op.drop_index("ix_telegram_conversation_messages_status", table_name="telegram_conversation_messages")
    columns = _message_columns()
    if "client_key" in columns:
        op.drop_column("telegram_conversation_messages", "client_key")
    if "status" in columns:
        op.drop_column("telegram_conversation_messages", "status")
