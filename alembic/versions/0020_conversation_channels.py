"""Separa o canal de exibição (Telegram ou app) sem perder o contexto compartilhado da IA.

Instalações novas já recebem a coluna em 0001 (create_all do modelo atual).
Esta revisão cobre bancos que pararam em 0018.
"""
from alembic import op
import sqlalchemy as sa

revision = "0020_conversation_channels"
down_revision = "0018_draft_fuel_details"
branch_labels = None
depends_on = None


def _columns():
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns("telegram_conversation_messages")}


def _indexes():
    return {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("telegram_conversation_messages")}


def upgrade() -> None:
    if "channel" not in _columns():
        op.add_column("telegram_conversation_messages", sa.Column(
            "channel", sa.String(length=20), nullable=False, server_default="telegram"))
    if "ix_telegram_conversation_messages_channel" not in _indexes():
        op.create_index("ix_telegram_conversation_messages_channel",
                        "telegram_conversation_messages", ["channel"], unique=False)


def downgrade() -> None:
    if "ix_telegram_conversation_messages_channel" in _indexes():
        op.drop_index("ix_telegram_conversation_messages_channel", table_name="telegram_conversation_messages")
    if "channel" in _columns():
        op.drop_column("telegram_conversation_messages", "channel")
