"""GiST index for "jobs running on day D" (utilization rollup)

The daily_partition_util rollup joins each recomputed day against every job whose [start, end)
overlaps it. With btree indexes only one side of that overlap can be indexed, so each day cost a
scan of the whole retention window. A GiST index on tstzrange(start_time, end_time) answers the
&& overlap test directly. Partial so that rows without a start, or with start > end (which
tstzrange() rejects), are simply never indexed.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-23
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_jobs_active_range ON jobs USING gist (tstzrange(start_time, end_time)) "
        "WHERE start_time IS NOT NULL AND start_time <= end_time"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_jobs_active_range")
