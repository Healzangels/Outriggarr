"""subscription.auto_download + created_at: what the scheduler queues by itself

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-02

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("subscription") as batch:
        # existing subscriptions keep today's behaviour: everything Sonarr wants
        batch.add_column(
            sa.Column("auto_download", sa.String(length=10), nullable=False, server_default="all")
        )
        batch.add_column(
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now())
        )


def downgrade() -> None:
    with op.batch_alter_table("subscription") as batch:
        batch.drop_column("created_at")
        batch.drop_column("auto_download")
