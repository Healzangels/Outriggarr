"""initial: connection, setting, job

Revision ID: 0001
Revises:
Create Date: 2026-09-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "connection",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.Enum("sonarr", "radarr", name="connectionkind"), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("url", sa.String(length=500), nullable=False),
        sa.Column("api_key", sa.String(length=200), nullable=False),
        sa.Column("staging_path_remote", sa.String(length=500), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "setting",
        sa.Column("key", sa.String(length=100), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
    )
    op.create_table(
        "job",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("connection_id", sa.Integer(), sa.ForeignKey("connection.id"), nullable=False),
        sa.Column("target_kind", sa.Enum("episode", "movie", name="targetkind"), nullable=False),
        sa.Column("series_id", sa.Integer(), nullable=True),
        sa.Column("episode_ids", sa.JSON(), nullable=True),
        sa.Column("movie_id", sa.Integer(), nullable=True),
        sa.Column("target_key", sa.String(length=200), nullable=False),
        sa.Column("video_id", sa.String(length=100), nullable=False),
        sa.Column("video_url", sa.String(length=1000), nullable=False),
        sa.Column("video_title", sa.String(length=500), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "downloading",
                "importing",
                "done",
                "failed",
                "cancelled",
                name="jobstatus",
            ),
            nullable=False,
        ),
        sa.Column("progress_pct", sa.Integer(), nullable=False),
        sa.Column("staged_path", sa.String(length=1000), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("connection_id", "target_key", "video_id", name="uq_job_target_video"),
    )
    op.create_index("ix_job_status", "job", ["status"])


def downgrade() -> None:
    op.drop_index("ix_job_status", table_name="job")
    op.drop_table("job")
    op.drop_table("setting")
    op.drop_table("connection")
