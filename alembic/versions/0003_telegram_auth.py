"""Adiciona vinculacao segura entre usuarios e Telegram."""
from alembic import op

from app.database import Base
from app import models  # noqa: F401

revision = "0003_telegram_auth"
down_revision = "0002_seed"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    Base.metadata.tables["telegram_links"].create(bind=bind, checkfirst=True)
    Base.metadata.tables["telegram_pairing_codes"].create(bind=bind, checkfirst=True)


def downgrade():
    bind = op.get_bind()
    Base.metadata.tables["telegram_pairing_codes"].drop(bind=bind, checkfirst=True)
    Base.metadata.tables["telegram_links"].drop(bind=bind, checkfirst=True)
