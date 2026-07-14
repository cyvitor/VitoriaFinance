"""adiciona memoria permanente por usuario"""
from alembic import op

from app.database import Base
from app import models  # noqa: F401

revision = "0008_user_memories"
down_revision = "0007_telegram_financial_agent"
branch_labels = None
depends_on = None


def upgrade():
    Base.metadata.tables["user_memories"].create(bind=op.get_bind(), checkfirst=True)


def downgrade():
    Base.metadata.tables["user_memories"].drop(bind=op.get_bind(), checkfirst=True)
