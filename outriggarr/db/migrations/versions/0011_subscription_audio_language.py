"""subscription.audio_language: per-series audio tag override

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-02

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("subscription") as batch:
        batch.add_column(sa.Column("audio_language", sa.String(length=3), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("subscription") as batch:
        batch.drop_column("audio_language")
