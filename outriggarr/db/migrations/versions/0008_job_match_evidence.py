"""job.matched_by / video_duration / target_runtime: how a job was matched, for review

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-02

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("job") as batch:
        batch.add_column(sa.Column("matched_by", sa.String(length=20), nullable=True))
        batch.add_column(sa.Column("video_duration", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("target_runtime", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("job") as batch:
        batch.drop_column("target_runtime")
        batch.drop_column("video_duration")
        batch.drop_column("matched_by")
