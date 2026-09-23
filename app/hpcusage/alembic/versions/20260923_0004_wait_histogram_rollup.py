"""Queue-wait histogram in daily_partition_util

/api/v1/wait_times built its histogram by bucketing every per-job row in the window on each
request, a scan that grew with the month's job count. The counts are now rolled up per
(day, partition) next to wait_p50_s/wait_p90_s, attributed to the day the job started, and
backfilled here from the per-job rows still within retention.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

# Frozen copy of hpcusage.models.WAIT_BUCKETS at this revision: (column, lower bound s, upper bound s).
WAIT_BUCKETS = [
    ("wait_lt_1m", 0, 60), ("wait_1m_10m", 60, 600), ("wait_10m_1h", 600, 3600),
    ("wait_1h_6h", 3600, 21600), ("wait_6h_1d", 21600, 86400), ("wait_gt_1d", 86400, None),
]


def upgrade() -> None:
    for col, *_ in WAIT_BUCKETS:
        op.add_column("daily_partition_util", sa.Column(col, sa.Integer(), nullable=False, server_default="0"))
    counts = ",\n".join(
        f"  count(*) FILTER (WHERE j.wait_s >= {lo}{f' AND j.wait_s < {hi}' if hi is not None else ''}) AS {col}"
        for col, lo, hi in WAIT_BUCKETS
    )
    sets = ", ".join(f"{col} = h.{col}" for col, *_ in WAIT_BUCKETS)
    op.execute(f"""
UPDATE daily_partition_util u SET {sets}
FROM (
  SELECT j.cluster_id, (j.start_time AT TIME ZONE c.timezone)::date AS day, j.partition,
{counts}
  FROM jobs j JOIN clusters c ON c.id = j.cluster_id
  WHERE j.start_time IS NOT NULL AND j.start_time <= j.end_time AND j.elapsed_s > 0
  GROUP BY 1, 2, 3
) h
WHERE u.cluster_id = h.cluster_id AND u.day = h.day AND u.partition = h.partition
""")


def downgrade() -> None:
    for col, *_ in WAIT_BUCKETS:
        op.drop_column("daily_partition_util", col)
