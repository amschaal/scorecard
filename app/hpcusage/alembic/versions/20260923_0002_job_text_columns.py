"""widen Slurm-sourced job strings to text

sacct values have no fixed length: a job cancelled while pending reports every
requested partition ("high,med,low,gpu-a100,...") and a pending array's JobID can
be a long task list ("123_[1,4,7-20,...]"), overflowing varchar(64). varchar -> text
is binary-coercible in Postgres, so this is a catalog-only change (no rewrite).

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# table -> [(column, previous varchar length)]
COLUMNS = {
    "jobs": [
        ("job_id_raw", 64), ("job_id", 64), ("user_name", 64), ("account", 128),
        ("partition", 64), ("qos", 64), ("job_name", 128), ("state", 32),
        ("gpu_type", 64), ("reason", 128),
    ],
    "daily_usage": [("user_name", 64), ("account", 128), ("partition", 64), ("qos", 64)],
    "daily_partition_util": [("partition", 64)],
}


def upgrade() -> None:
    for table, cols in COLUMNS.items():
        for col, n in cols:
            op.alter_column(table, col, type_=sa.Text(), existing_type=sa.String(n))


def downgrade() -> None:
    # Truncates any values that no longer fit.
    for table, cols in COLUMNS.items():
        for col, n in cols:
            op.alter_column(table, col, type_=sa.String(n), existing_type=sa.Text(),
                            postgresql_using=f'left("{col}", {n})')
