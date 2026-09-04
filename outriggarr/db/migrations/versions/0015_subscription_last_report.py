"""subscription.last_report: the cached preview, so a page open costs no listing

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-03

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("subscription") as batch:
        batch.add_column(sa.Column("last_report", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("subscription") as batch:
        batch.drop_column("last_report")
