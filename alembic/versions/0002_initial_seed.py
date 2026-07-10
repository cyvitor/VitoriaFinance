"""Seed inicial: conta VH, superadministrador vh e categorias."""
from alembic import op
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import SystemAccount, User
from scripts.seed import seed_session

revision = "0002_seed"
down_revision = "0001_schema"
branch_labels = None
depends_on = None


def upgrade():
    with Session(bind=op.get_bind()) as db:
        seed_session(db)


def downgrade():
    with Session(bind=op.get_bind()) as db:
        user = db.scalar(select(User).where(User.username == "vh"))
        if user:
            db.delete(user)
            db.flush()
        account = db.scalar(select(SystemAccount).where(SystemAccount.name == "VH"))
        if account:
            db.delete(account)
        db.commit()
