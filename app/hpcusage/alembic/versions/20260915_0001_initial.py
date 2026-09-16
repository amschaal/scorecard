"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-15
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "clusters",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "ingest_batches",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("cluster_id", sa.Integer(), sa.ForeignKey("clusters.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True)),
        sa.Column("window_end", sa.DateTime(timezone=True)),
        sa.Column("taken_at", sa.DateTime(timezone=True)),
        sa.Column("rows_received", sa.Integer(), nullable=False),
        sa.Column("rows_inserted", sa.Integer(), nullable=False),
        sa.Column("rows_updated", sa.Integer(), nullable=False),
        sa.Column("collector_version", sa.String(32)),
        sa.Column("slurm_version", sa.String(64)),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error", sa.Text()),
    )
    op.create_index("ix_ingest_batches_cluster_kind_received", "ingest_batches", ["cluster_id", "kind", "received_at"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("cluster_id", sa.Integer(), sa.ForeignKey("clusters.id"), nullable=False),
        sa.Column("job_id_raw", sa.String(64), nullable=False),
        sa.Column("job_id", sa.String(64), nullable=False),
        sa.Column("array_job_id", sa.BigInteger()),
        sa.Column("array_task_id", sa.Integer()),
        sa.Column("het_job_id", sa.BigInteger()),
        sa.Column("het_offset", sa.Integer()),
        sa.Column("user_name", sa.String(64), nullable=False),
        sa.Column("account", sa.String(128), nullable=False),
        sa.Column("partition", sa.String(64), nullable=False),
        sa.Column("qos", sa.String(64), nullable=False),
        sa.Column("job_name", sa.String(128)),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("cancelled_by", sa.Integer()),
        sa.Column("exit_code", sa.Integer()),
        sa.Column("signal", sa.Integer()),
        sa.Column("submit_time", sa.DateTime(timezone=True)),
        sa.Column("start_time", sa.DateTime(timezone=True)),
        sa.Column("end_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_day", sa.Date(), nullable=False),
        sa.Column("elapsed_s", sa.Integer(), nullable=False),
        sa.Column("timelimit_s", sa.Integer()),
        sa.Column("wait_s", sa.Integer()),
        sa.Column("alloc_cpus", sa.Integer(), nullable=False),
        sa.Column("req_cpus", sa.Integer(), nullable=False),
        sa.Column("nnodes", sa.Integer(), nullable=False),
        sa.Column("node_list", sa.Text()),
        sa.Column("alloc_mem_mb", sa.BigInteger()),
        sa.Column("req_mem_mb", sa.BigInteger()),
        sa.Column("max_rss_mb", sa.BigInteger()),
        sa.Column("mem_eff_approx", sa.Boolean(), nullable=False),
        sa.Column("total_cpu_s", sa.Float()),
        sa.Column("cpu_seconds_alloc", sa.BigInteger(), nullable=False),
        sa.Column("gpus", sa.Integer()),
        sa.Column("gpu_type", sa.String(64)),
        sa.Column("billing", sa.Numeric(14, 3)),
        sa.Column("ntasks", sa.Integer()),
        sa.Column("alloc_tres", sa.Text()),
        sa.Column("req_tres", sa.Text()),
        sa.Column("reason", sa.String(128)),
        sa.Column("ingest_batch_id", sa.BigInteger(), sa.ForeignKey("ingest_batches.id")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("cluster_id", "job_id_raw", name="uq_jobs_cluster_jobid"),
    )
    op.create_index("ix_jobs_cluster_end_day", "jobs", ["cluster_id", "end_day"])
    op.create_index("ix_jobs_cluster_end_time", "jobs", ["cluster_id", "end_time"])
    op.create_index("ix_jobs_cluster_start_time", "jobs", ["cluster_id", "start_time"])
    op.create_index("ix_jobs_cluster_user_end_day", "jobs", ["cluster_id", "user_name", "end_day"])
    op.create_index("ix_jobs_cluster_account_end_day", "jobs", ["cluster_id", "account", "end_day"])
    op.create_index("ix_jobs_cluster_partition_end_day", "jobs", ["cluster_id", "partition", "end_day"])

    op.create_table(
        "daily_usage",
        sa.Column("cluster_id", sa.Integer(), sa.ForeignKey("clusters.id"), primary_key=True),
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("user_name", sa.String(64), primary_key=True),
        sa.Column("account", sa.String(128), primary_key=True),
        sa.Column("partition", sa.String(64), primary_key=True),
        sa.Column("qos", sa.String(64), primary_key=True),
        sa.Column("job_count", sa.Integer(), nullable=False),
        sa.Column("jobs_completed", sa.Integer(), nullable=False),
        sa.Column("jobs_failed", sa.Integer(), nullable=False),
        sa.Column("jobs_timeout", sa.Integer(), nullable=False),
        sa.Column("jobs_cancelled", sa.Integer(), nullable=False),
        sa.Column("jobs_oom", sa.Integer(), nullable=False),
        sa.Column("jobs_node_fail", sa.Integer(), nullable=False),
        sa.Column("jobs_preempted", sa.Integer(), nullable=False),
        sa.Column("jobs_under_5min", sa.Integer(), nullable=False),
        sa.Column("cpu_seconds_alloc", sa.BigInteger(), nullable=False),
        sa.Column("cpu_seconds_used", sa.Float(), nullable=False),
        sa.Column("gpu_seconds", sa.BigInteger(), nullable=False),
        sa.Column("mem_mb_seconds", sa.BigInteger(), nullable=False),
        sa.Column("node_seconds", sa.BigInteger(), nullable=False),
        sa.Column("wall_seconds", sa.BigInteger(), nullable=False),
        sa.Column("billing_seconds", sa.Float(), nullable=False),
        sa.Column("wait_seconds_sum", sa.BigInteger(), nullable=False),
        sa.Column("jobs_with_wait", sa.Integer(), nullable=False),
        sa.Column("elapsed_limited_sum", sa.BigInteger(), nullable=False),
        sa.Column("timelimit_s_sum", sa.BigInteger(), nullable=False),
        sa.Column("max_rss_mb_sum", sa.BigInteger(), nullable=False),
        sa.Column("alloc_mem_mb_sum", sa.BigInteger(), nullable=False),
        sa.Column("jobs_with_rss", sa.Integer(), nullable=False),
    )
    op.create_index("ix_daily_usage_cluster_day", "daily_usage", ["cluster_id", "day"])
    op.create_index("ix_daily_usage_cluster_user", "daily_usage", ["cluster_id", "user_name"])
    op.create_index("ix_daily_usage_cluster_account", "daily_usage", ["cluster_id", "account"])

    op.create_table(
        "daily_partition_util",
        sa.Column("cluster_id", sa.Integer(), sa.ForeignKey("clusters.id"), primary_key=True),
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("partition", sa.String(64), primary_key=True),
        sa.Column("cpu_seconds", sa.Float(), nullable=False),
        sa.Column("gpu_seconds", sa.Float(), nullable=False),
        sa.Column("mem_mb_seconds", sa.Float(), nullable=False),
        sa.Column("node_seconds", sa.Float(), nullable=False),
        sa.Column("capacity_cpu_seconds", sa.Float()),
        sa.Column("capacity_gpu_seconds", sa.Float()),
        sa.Column("capacity_mem_mb_seconds", sa.Float()),
        sa.Column("capacity_node_seconds", sa.Float()),
        sa.Column("jobs_started", sa.Integer(), nullable=False),
        sa.Column("wait_p50_s", sa.Float()),
        sa.Column("wait_p90_s", sa.Float()),
        sa.Column("wait_mean_s", sa.Float()),
    )

    op.create_table(
        "node_snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("cluster_id", sa.Integer(), sa.ForeignKey("clusters.id"), nullable=False),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("node_name", sa.String(64), nullable=False),
        sa.Column("state", sa.String(64), nullable=False),
        sa.Column("cpus_total", sa.Integer(), nullable=False),
        sa.Column("cpus_alloc", sa.Integer(), nullable=False),
        sa.Column("mem_mb_total", sa.BigInteger(), nullable=False),
        sa.Column("mem_mb_alloc", sa.BigInteger(), nullable=False),
        sa.Column("gpus_total", sa.Integer(), nullable=False),
        sa.Column("gpus_alloc", sa.Integer(), nullable=False),
        sa.Column("gpu_type", sa.String(64)),
        sa.Column("partitions", postgresql.ARRAY(sa.String(64)), nullable=False),
        sa.Column("features", sa.Text()),
    )
    op.create_index("ix_node_snapshots_cluster_taken", "node_snapshots", ["cluster_id", "taken_at"])

    op.create_table(
        "partition_snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("cluster_id", sa.Integer(), sa.ForeignKey("clusters.id"), nullable=False),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("partition", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("total_cpus", sa.Integer()),
        sa.Column("total_nodes", sa.Integer()),
        sa.Column("max_time_s", sa.BigInteger()),
        sa.Column("default_time_s", sa.BigInteger()),
        sa.Column("nodes", sa.Text()),
    )
    op.create_index("ix_partition_snapshots_cluster_taken", "partition_snapshots", ["cluster_id", "taken_at"])

    op.create_table(
        "fairshare_snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("cluster_id", sa.Integer(), sa.ForeignKey("clusters.id"), nullable=False),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("account", sa.String(128), nullable=False),
        sa.Column("user_name", sa.String(64)),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("raw_shares", sa.Float()),
        sa.Column("shares_parent", sa.Boolean(), nullable=False),
        sa.Column("norm_shares", sa.Float()),
        sa.Column("raw_usage", sa.Float()),
        sa.Column("norm_usage", sa.Float()),
        sa.Column("effective_usage", sa.Float()),
        sa.Column("fairshare", sa.Float()),
        sa.Column("level_fs", sa.Float()),
    )
    op.create_index("ix_fairshare_snapshots_cluster_acct_user_taken", "fairshare_snapshots",
                    ["cluster_id", "account", "user_name", "taken_at"])


def downgrade() -> None:
    for t in ("fairshare_snapshots", "partition_snapshots", "node_snapshots", "daily_partition_util",
              "daily_usage", "jobs", "ingest_batches", "clusters"):
        op.drop_table(t)
