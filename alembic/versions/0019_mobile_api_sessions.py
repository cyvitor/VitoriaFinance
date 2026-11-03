"""persistent bearer sessions for the React Native app"""
from alembic import op
import sqlalchemy as sa


revision = "0019_mobile_api_sessions"
down_revision = "0018_draft_fuel_details"
branch_labels = None
depends_on = None


def upgrade():
    # The baseline migration builds its schema from current model metadata.
    # Fresh installs can therefore already have this table before this revision.
    if "mobile_api_sessions" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "mobile_api_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("device_name", sa.String(length=120), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_mobile_api_sessions_user_id", "mobile_api_sessions", ["user_id"])
    op.create_index("ix_mobile_api_sessions_token_hash", "mobile_api_sessions", ["token_hash"], unique=True)
    op.create_index("ix_mobile_api_sessions_expires_at", "mobile_api_sessions", ["expires_at"])


def downgrade():
    if "mobile_api_sessions" not in sa.inspect(op.get_bind()).get_table_names():
        return
    op.drop_index("ix_mobile_api_sessions_expires_at", table_name="mobile_api_sessions")
    op.drop_index("ix_mobile_api_sessions_token_hash", table_name="mobile_api_sessions")
    op.drop_index("ix_mobile_api_sessions_user_id", table_name="mobile_api_sessions")
    op.drop_table("mobile_api_sessions")
