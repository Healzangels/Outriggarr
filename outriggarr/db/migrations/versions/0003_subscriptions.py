"""subscription + override tables; job.subscription_id, job.format

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "subscription",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("connection_id", sa.Integer(), sa.ForeignKey("connection.id"), nullable=False),
        sa.Column("series_id", sa.Integer(), nullable=False),
        sa.Column("tvdb_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("source_url", sa.String(length=1000), nullable=False),
        sa.Column("format", sa.String(length=500), nullable=True),
        sa.Column("strategies", sa.JSON(), nullable=False),
        sa.Column("date_tolerance_days", sa.Integer(), nullable=False),
        sa.Column("date_offset_days", sa.Integer(), nullable=False),
        sa.Column("title_regex", sa.String(length=500), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_scan_at", sa.DateTime(), nullable=True),
        sa.Column("last_scan_result", sa.JSON(), nullable=True),
        sa.UniqueConstraint("connection_id", "series_id", name="uq_subscription_series"),
    )
    op.create_table(
        "override",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "subscription_id",
            sa.Integer(),
            sa.ForeignKey("subscription.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("video_id", sa.String(length=100), nullable=False),
        sa.Column("season", sa.Integer(), nullable=False),
        sa.Column("episode", sa.Integer(), nullable=False),
        sa.UniqueConstraint("subscription_id", "video_id", name="uq_override_video"),
    )
    with op.batch_alter_table("job") as batch:
        batch.add_column(sa.Column("subscription_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("format", sa.String(length=500), nullable=True))
        batch.create_foreign_key(
            "fk_job_subscription", "subscription", ["subscription_id"], ["id"], ondelete="SET NULL"
        )


def downgrade() -> None:
    with op.batch_alter_table("job") as batch:
        batch.drop_constraint("fk_job_subscription", type_="foreignkey")
        batch.drop_column("format")
        batch.drop_column("subscription_id")
    op.drop_table("override")
    op.drop_table("subscription")
