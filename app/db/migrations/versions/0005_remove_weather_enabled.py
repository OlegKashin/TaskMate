"""Remove weather_enabled column from user_settings if present."""

import sqlalchemy as sa
from alembic import op

revision = "0005_remove_weather_enabled"
down_revision = "0004_required_unique_indexes"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [col["name"] for col in inspector.get_columns("user_settings")]
    if "weather_enabled" in columns:
        op.drop_column("user_settings", "weather_enabled")


def downgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [col["name"] for col in inspector.get_columns("user_settings")]
    if "weather_enabled" not in columns:
        op.add_column(
            "user_settings",
            sa.Column("weather_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        )
