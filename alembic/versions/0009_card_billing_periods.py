"""Adiciona fechamento manual das faturas de cartão."""
from alembic import op

from app.database import Base
from app import models  # noqa: F401

revision = "0009_card_billing_periods"
down_revision = "0008_user_memories"
branch_labels = None
depends_on = None


def upgrade():
    Base.metadata.tables["card_billing_periods"].create(bind=op.get_bind(), checkfirst=True)


def downgrade():
    Base.metadata.tables["card_billing_periods"].drop(bind=op.get_bind(), checkfirst=True)
