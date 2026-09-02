"""video_meta: cached per-video upload dates

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-02

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "video_meta",
        sa.Column("video_id", sa.String(length=100), primary_key=True),
        sa.Column("upload_date", sa.String(length=8), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("video_meta")
