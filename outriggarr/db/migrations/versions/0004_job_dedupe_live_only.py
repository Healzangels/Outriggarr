"""job dedupe applies to non-done jobs only (partial unique index)

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-02

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("job") as batch:
        batch.drop_constraint("uq_job_target_video", type_="unique")
    op.create_index(
        "ux_job_live_target_video",
        "job",
        ["connection_id", "target_key", "video_id"],
        unique=True,
        sqlite_where=sa.text("status != 'done'"),
    )


def downgrade() -> None:
    # Re-create the full constraint BEFORE dropping the partial index, so a done+live
    # twin (allowed at 0004) fails the downgrade with the index still in place instead
    # of leaving no dedupe at all.
    with op.batch_alter_table("job") as batch:
        batch.create_unique_constraint(
            "uq_job_target_video", ["connection_id", "target_key", "video_id"]
        )
    op.drop_index("ux_job_live_target_video", table_name="job")
