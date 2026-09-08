"""adiciona veículos compartilhados e abastecimentos

Revision ID: 0014_vehicles_and_fuel_fillups
Revises: 0013_financing_contract_type
"""
from alembic import op
import sqlalchemy as sa

revision = "0014_vehicles_and_fuel_fillups"
down_revision = "0013_financing_contract_type"
branch_labels = None
depends_on = None

def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "vehicles" not in inspector.get_table_names():
        op.create_table("vehicles", sa.Column("id", sa.Integer, primary_key=True), sa.Column("workspace_id", sa.Integer, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False), sa.Column("name", sa.String(120), nullable=False), sa.Column("year", sa.Integer), sa.Column("initial_odometer_km", sa.Numeric(12, 1), nullable=False, server_default="0"), sa.Column("fuel_type", sa.String(40)), sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()), sa.Column("created_at", sa.DateTime, nullable=False), sa.Index("ix_vehicles_workspace_id", "workspace_id"))
    if "fuel_fillups" not in inspector.get_table_names():
        op.create_table("fuel_fillups", sa.Column("id", sa.Integer, primary_key=True), sa.Column("workspace_id", sa.Integer, sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False), sa.Column("vehicle_id", sa.Integer, sa.ForeignKey("vehicles.id", ondelete="CASCADE"), nullable=False), sa.Column("transaction_id", sa.Integer, sa.ForeignKey("transactions.id", ondelete="CASCADE"), nullable=True, unique=True), sa.Column("fillup_date", sa.Date, nullable=False), sa.Column("odometer_km", sa.Numeric(12, 1), nullable=False), sa.Column("liters", sa.Numeric(12, 3), nullable=False), sa.Column("price_per_liter", sa.Numeric(12, 3), nullable=False), sa.Column("station", sa.String(120)), sa.Column("notes", sa.Text), sa.Column("created_at", sa.DateTime, nullable=False), sa.Index("ix_fuel_fillups_workspace_id", "workspace_id"), sa.Index("ix_fuel_fillups_vehicle_id", "vehicle_id"), sa.Index("ix_fuel_fillups_transaction_id", "transaction_id"), sa.Index("ix_fuel_fillups_fillup_date", "fillup_date"))

def downgrade():
    op.drop_table("fuel_fillups")
    op.drop_table("vehicles")
