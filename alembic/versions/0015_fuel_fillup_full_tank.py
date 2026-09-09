"""adiciona indicador de tanque cheio aos abastecimentos

Revision ID: 0015_fuel_fillup_full_tank
Revises: 0014_vehicles_and_fuel_fillups
"""
from alembic import op
import sqlalchemy as sa

revision = "0015_fuel_fillup_full_tank"
down_revision = "0014_vehicles_and_fuel_fillups"
branch_labels = None
depends_on = None


def upgrade():
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("fuel_fillups")}
    if "is_full_tank" not in columns:
        op.add_column("fuel_fillups", sa.Column(
            "is_full_tank", sa.Boolean(), nullable=False, server_default=sa.false(),
        ))


def downgrade():
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("fuel_fillups")}
    if "is_full_tank" in columns:
        op.drop_column("fuel_fillups", "is_full_tank")
