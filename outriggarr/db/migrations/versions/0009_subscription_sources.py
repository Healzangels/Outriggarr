"""subscription.sources: one or more source URLs (replaces source_url)

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-02

"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("subscription") as batch:
        batch.add_column(sa.Column("sources", sa.JSON(), nullable=False, server_default="[]"))
    conn = op.get_bind()
    for sid, url in conn.execute(sa.text("SELECT id, source_url FROM subscription")).fetchall():
        conn.execute(
            sa.text("UPDATE subscription SET sources = :s WHERE id = :id"),
            {"s": json.dumps([url] if url else []), "id": sid},
        )
    with op.batch_alter_table("subscription") as batch:
        batch.drop_column("source_url")


def downgrade() -> None:
    with op.batch_alter_table("subscription") as batch:
        batch.add_column(sa.Column("source_url", sa.String(length=1000), nullable=True))
    conn = op.get_bind()
    for sid, raw in conn.execute(sa.text("SELECT id, sources FROM subscription")).fetchall():
        urls = json.loads(raw) if raw else []
        conn.execute(
            sa.text("UPDATE subscription SET source_url = :u WHERE id = :id"),
            {"u": urls[0] if urls else "", "id": sid},
        )
    with op.batch_alter_table("subscription") as batch:
        batch.alter_column("source_url", nullable=False)
        batch.drop_column("sources")
