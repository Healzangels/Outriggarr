"""override.video_url / video_title

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-02

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("override") as batch:
        batch.add_column(sa.Column("video_url", sa.String(length=1000), nullable=True))
        batch.add_column(sa.Column("video_title", sa.String(length=500), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("override") as batch:
        batch.drop_column("video_title")
        batch.drop_column("video_url")
