"""Index jobs on (cluster_id, end_day, end_time)

The jobs page lists a window newest-first as ORDER BY end_day DESC, end_time DESC. With the index
ending at end_day, Postgres had to fetch and sort every job that ended on the newest day before
it could return the first page. Adding end_time makes that order an index walk that stops after
LIMIT rows. The wider index serves every query the old (cluster_id, end_day) one did, so the old
one is dropped rather than kept alongside.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-23
"""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_jobs_cluster_end_day_end_time", "jobs", ["cluster_id", "end_day", "end_time"])
    op.drop_index("ix_jobs_cluster_end_day", table_name="jobs")


def downgrade() -> None:
    op.create_index("ix_jobs_cluster_end_day", "jobs", ["cluster_id", "end_day"])
    op.drop_index("ix_jobs_cluster_end_day_end_time", table_name="jobs")
