"""nível de acesso por área financeira

Revision ID: 0016_area_access_level
Revises: 0015_fuel_fillup_full_tank
"""
from alembic import op
import sqlalchemy as sa

revision = "0016_area_access_level"
down_revision = "0015_fuel_fillup_full_tank"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns("user_person_access")}
    if "access_level" in columns:
        return
    column_type = (
        sa.Enum("read", "write", name="areaaccesslevel")
        if bind.dialect.name == "mysql"
        else sa.String(length=10)
    )
    op.add_column("user_person_access", sa.Column(
        "access_level", column_type, nullable=False, server_default="write",
    ))


def downgrade():
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns("user_person_access")}
    if "access_level" in columns:
        op.drop_column("user_person_access", "access_level")
