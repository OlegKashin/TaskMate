"""Enforce active project and task calendar event uniqueness."""

import sqlalchemy as sa
from alembic import op

revision = "0004_required_unique_indexes"
down_revision = "0003_calendar_retry_state"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ux_projects_user_name_active", "projects", ["user_id", "name"], unique=True,
        postgresql_where=sa.text("is_archived = false"),
        sqlite_where=sa.text("is_archived = 0"),
    )
    op.create_index(
        "ux_calendar_events_task_active", "calendar_events", ["task_id"], unique=True,
        postgresql_where=sa.text("task_id IS NOT NULL AND status <> 'cancelled'"),
        sqlite_where=sa.text("task_id IS NOT NULL AND status <> 'cancelled'"),
    )


def downgrade():
    op.drop_index("ux_calendar_events_task_active", table_name="calendar_events")
    op.drop_index("ux_projects_user_name_active", table_name="projects")
