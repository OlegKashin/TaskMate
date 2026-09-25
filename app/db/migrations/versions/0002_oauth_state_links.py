"""Link OAuth state to its connecting source or calendar."""

import sqlalchemy as sa
from alembic import op

revision = "0002_oauth_state_links"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    # Existing 0001 installs used live metadata; fresh static 0001 already has these columns.
    if "purpose" in {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("oauth_states")
    }:
        return
    op.add_column("oauth_states", sa.Column("purpose", sa.String(16), nullable=True))
    op.add_column("oauth_states", sa.Column("source_id", sa.Uuid(), nullable=True))
    op.add_column("oauth_states", sa.Column("calendar_connection_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_oauth_state_source",
        "oauth_states",
        "sources",
        ["source_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_oauth_state_calendar",
        "oauth_states",
        "calendar_connections",
        ["calendar_connection_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute(
        "UPDATE oauth_states SET purpose = CASE WHEN provider = 'google' THEN 'calendar' ELSE 'source' END"
    )
    op.alter_column("oauth_states", "purpose", nullable=False)


def downgrade():
    # 0001's frozen schema includes these columns; rolling back the compatibility
    # revision must not make the schema older than 0001 declares.
    pass
