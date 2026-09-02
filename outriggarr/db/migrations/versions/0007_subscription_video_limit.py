"""subscription.video_limit: per-subscription listing depth

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-02

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("subscription") as batch:
        batch.add_column(sa.Column("video_limit", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("subscription") as batch:
        batch.drop_column("video_limit")
