"""SQLAlchemy 2.x models. The Alembic migrations in app/alembic/versions mirror these;
tests/test_migrations.py asserts they stay in sync."""

from datetime import date, datetime

from sqlalchemy import (
    ARRAY, BigInteger, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, Numeric,
    String, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Cluster(Base):
    __tablename__ = "clusters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="America/Los_Angeles")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class IngestBatch(Base):
    __tablename__ = "ingest_batches"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cluster_id: Mapped[int] = mapped_column(ForeignKey("clusters.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    taken_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rows_received: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_inserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    collector_version: Mapped[str | None] = mapped_column(String(32))
    slurm_version: Mapped[str | None] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ok")
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_ingest_batches_cluster_kind_received", "cluster_id", "kind", "received_at"),)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cluster_id: Mapped[int] = mapped_column(ForeignKey("clusters.id"), nullable=False)
    job_id_raw: Mapped[str] = mapped_column(Text, nullable=False)
    job_id: Mapped[str] = mapped_column(Text, nullable=False)
    array_job_id: Mapped[int | None] = mapped_column(BigInteger)
    array_task_id: Mapped[int | None] = mapped_column(Integer)
    het_job_id: Mapped[int | None] = mapped_column(BigInteger)
    het_offset: Mapped[int | None] = mapped_column(Integer)
    user_name: Mapped[str] = mapped_column(Text, nullable=False)
    account: Mapped[str] = mapped_column(Text, nullable=False, default="")
    partition: Mapped[str] = mapped_column(Text, nullable=False, default="")
    qos: Mapped[str] = mapped_column(Text, nullable=False, default="")
    job_name: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    cancelled_by: Mapped[int | None] = mapped_column(Integer)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    signal: Mapped[int | None] = mapped_column(Integer)
    submit_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_day: Mapped[date] = mapped_column(Date, nullable=False)  # end_time in the cluster's timezone
    elapsed_s: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    timelimit_s: Mapped[int | None] = mapped_column(Integer)
    wait_s: Mapped[int | None] = mapped_column(Integer)
    alloc_cpus: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    req_cpus: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    nnodes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    node_list: Mapped[str | None] = mapped_column(Text)
    alloc_mem_mb: Mapped[int | None] = mapped_column(BigInteger)
    req_mem_mb: Mapped[int | None] = mapped_column(BigInteger)
    max_rss_mb: Mapped[int | None] = mapped_column(BigInteger)
    mem_eff_approx: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    total_cpu_s: Mapped[float | None] = mapped_column(Float)
    cpu_seconds_alloc: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    gpus: Mapped[int | None] = mapped_column(Integer)
    gpu_type: Mapped[str | None] = mapped_column(Text)
    billing: Mapped[float | None] = mapped_column(Numeric(14, 3))
    ntasks: Mapped[int | None] = mapped_column(Integer)
    alloc_tres: Mapped[str | None] = mapped_column(Text)
    req_tres: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    ingest_batch_id: Mapped[int | None] = mapped_column(ForeignKey("ingest_batches.id"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("cluster_id", "job_id_raw", name="uq_jobs_cluster_jobid"),
        Index("ix_jobs_cluster_end_day", "cluster_id", "end_day"),
        Index("ix_jobs_cluster_end_time", "cluster_id", "end_time"),
        Index("ix_jobs_cluster_start_time", "cluster_id", "start_time"),
        Index("ix_jobs_cluster_user_end_day", "cluster_id", "user_name", "end_day"),
        Index("ix_jobs_cluster_account_end_day", "cluster_id", "account", "end_day"),
        Index("ix_jobs_cluster_partition_end_day", "cluster_id", "partition", "end_day"),
    )


class DailyUsage(Base):
    """Per (cluster, day, user, account, partition, qos) rollup of finished jobs. Kept forever."""

    __tablename__ = "daily_usage"

    cluster_id: Mapped[int] = mapped_column(ForeignKey("clusters.id"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    user_name: Mapped[str] = mapped_column(Text, primary_key=True)
    account: Mapped[str] = mapped_column(Text, primary_key=True)
    partition: Mapped[str] = mapped_column(Text, primary_key=True)
    qos: Mapped[str] = mapped_column(Text, primary_key=True)
    job_count: Mapped[int] = mapped_column(Integer, nullable=False)
    jobs_completed: Mapped[int] = mapped_column(Integer, nullable=False)
    jobs_failed: Mapped[int] = mapped_column(Integer, nullable=False)
    jobs_timeout: Mapped[int] = mapped_column(Integer, nullable=False)
    jobs_cancelled: Mapped[int] = mapped_column(Integer, nullable=False)
    jobs_oom: Mapped[int] = mapped_column(Integer, nullable=False)
    jobs_node_fail: Mapped[int] = mapped_column(Integer, nullable=False)
    jobs_preempted: Mapped[int] = mapped_column(Integer, nullable=False)
    jobs_under_5min: Mapped[int] = mapped_column(Integer, nullable=False)
    cpu_seconds_alloc: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cpu_seconds_used: Mapped[float] = mapped_column(Float, nullable=False)
    gpu_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mem_mb_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False)
    node_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False)
    wall_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False)
    billing_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    wait_seconds_sum: Mapped[int] = mapped_column(BigInteger, nullable=False)
    jobs_with_wait: Mapped[int] = mapped_column(Integer, nullable=False)
    elapsed_limited_sum: Mapped[int] = mapped_column(BigInteger, nullable=False)
    timelimit_s_sum: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_rss_mb_sum: Mapped[int] = mapped_column(BigInteger, nullable=False)
    alloc_mem_mb_sum: Mapped[int] = mapped_column(BigInteger, nullable=False)
    jobs_with_rss: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        Index("ix_daily_usage_cluster_day", "cluster_id", "day"),
        Index("ix_daily_usage_cluster_user", "cluster_id", "user_name"),
        Index("ix_daily_usage_cluster_account", "cluster_id", "account"),
    )


class DailyPartitionUtil(Base):
    """Allocated resource-seconds split across calendar days vs. snapshot capacity."""

    __tablename__ = "daily_partition_util"

    cluster_id: Mapped[int] = mapped_column(ForeignKey("clusters.id"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    partition: Mapped[str] = mapped_column(Text, primary_key=True)
    cpu_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    gpu_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    mem_mb_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    node_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    capacity_cpu_seconds: Mapped[float | None] = mapped_column(Float)
    capacity_gpu_seconds: Mapped[float | None] = mapped_column(Float)
    capacity_mem_mb_seconds: Mapped[float | None] = mapped_column(Float)
    capacity_node_seconds: Mapped[float | None] = mapped_column(Float)
    jobs_started: Mapped[int] = mapped_column(Integer, nullable=False)
    wait_p50_s: Mapped[float | None] = mapped_column(Float)
    wait_p90_s: Mapped[float | None] = mapped_column(Float)
    wait_mean_s: Mapped[float | None] = mapped_column(Float)


class NodeSnapshot(Base):
    __tablename__ = "node_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cluster_id: Mapped[int] = mapped_column(ForeignKey("clusters.id"), nullable=False)
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    node_name: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    cpus_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cpus_alloc: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mem_mb_total: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    mem_mb_alloc: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    gpus_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gpus_alloc: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gpu_type: Mapped[str | None] = mapped_column(String(64))
    partitions: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False, default=list)
    features: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_node_snapshots_cluster_taken", "cluster_id", "taken_at"),)


class PartitionSnapshot(Base):
    __tablename__ = "partition_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cluster_id: Mapped[int] = mapped_column(ForeignKey("clusters.id"), nullable=False)
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    partition: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    total_cpus: Mapped[int | None] = mapped_column(Integer)
    total_nodes: Mapped[int | None] = mapped_column(Integer)
    max_time_s: Mapped[int | None] = mapped_column(BigInteger)
    default_time_s: Mapped[int | None] = mapped_column(BigInteger)
    nodes: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_partition_snapshots_cluster_taken", "cluster_id", "taken_at"),)


class FairshareSnapshot(Base):
    __tablename__ = "fairshare_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cluster_id: Mapped[int] = mapped_column(ForeignKey("clusters.id"), nullable=False)
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    account: Mapped[str] = mapped_column(String(128), nullable=False)
    user_name: Mapped[str | None] = mapped_column(String(64))
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    raw_shares: Mapped[float | None] = mapped_column(Float)
    shares_parent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    norm_shares: Mapped[float | None] = mapped_column(Float)
    raw_usage: Mapped[float | None] = mapped_column(Float)
    norm_usage: Mapped[float | None] = mapped_column(Float)
    effective_usage: Mapped[float | None] = mapped_column(Float)
    fairshare: Mapped[float | None] = mapped_column(Float)
    level_fs: Mapped[float | None] = mapped_column(Float)

    __table_args__ = (
        Index("ix_fairshare_snapshots_cluster_acct_user_taken", "cluster_id", "account", "user_name", "taken_at"),
    )
