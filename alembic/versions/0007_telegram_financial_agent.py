"""adiciona conversas, acoes pendentes e amortizacoes do agente"""
from alembic import op

from app.database import Base
from app import models  # noqa: F401

revision = "0007_telegram_financial_agent"
down_revision = "0006_financings"
branch_labels = None
depends_on = None


TABLES = (
    "telegram_conversation_messages",
    "telegram_pending_actions",
    "financing_amortizations",
)


def upgrade():
    bind = op.get_bind()
    for name in TABLES:
        Base.metadata.tables[name].create(bind=bind, checkfirst=True)


def downgrade():
    bind = op.get_bind()
    for name in reversed(TABLES):
        Base.metadata.tables[name].drop(bind=bind, checkfirst=True)
