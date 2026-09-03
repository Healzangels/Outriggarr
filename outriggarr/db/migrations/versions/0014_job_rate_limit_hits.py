"""job.rate_limit_hits: how often one video's download answered rate-limited in a row

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-03

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("job") as batch:
        batch.add_column(
            sa.Column("rate_limit_hits", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("job") as batch:
        batch.drop_column("rate_limit_hits")
