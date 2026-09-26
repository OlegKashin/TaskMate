"""Track pending Calendar sync attempts and next retry time."""

import sqlalchemy as sa
from alembic import op

revision = "0003_calendar_retry_state"
down_revision = "0002_oauth_state_links"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("calendar_events", sa.Column("sync_attempts", sa.Integer(), nullable=False,
                                                server_default="0"))
    op.add_column("calendar_events", sa.Column("next_sync_at", sa.DateTime(timezone=True),
                                                nullable=True))


def downgrade():
    op.drop_column("calendar_events", "next_sync_at")
    op.drop_column("calendar_events", "sync_attempts")
