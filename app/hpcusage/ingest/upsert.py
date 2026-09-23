from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, literal_column, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..models import FairshareSnapshot, Job, NodeSnapshot, PartitionSnapshot
from .schemas import FairshareRow, JobRow, NodeRow, PartitionRow

# Columns that are refreshed when a job is re-collected (everything except identity).
_JOB_UPDATE_COLS = [
    c.name for c in Job.__table__.columns
    if c.name not in ("id", "cluster_id", "job_id_raw", "updated_at")
]
# Columns whose change makes a re-collected row worth rewriting. The collector re-sends the last
# two days on every run, so most conflicts carry identical data; rewriting them anyway would
# double the write volume, touch every index and leave dead tuples for autovacuum. The batch id
# is excluded so an unchanged row keeps pointing at the batch that last changed it.
_JOB_COMPARE_COLS = [c for c in _JOB_UPDATE_COLS if c != "ingest_batch_id"]


def job_row_to_record(row: JobRow, cluster_id: int, tz: ZoneInfo, batch_id: int) -> dict:
    end_local = row.end.astimezone(tz)
    return {
        "cluster_id": cluster_id,
        "job_id_raw": row.job_id_raw,
        "job_id": row.job_id,
        "array_job_id": row.array_job_id,
        "array_task_id": row.array_task_id,
        "het_job_id": row.het_job_id,
        "het_offset": row.het_offset,
        "user_name": row.user or "",
        "account": row.account or "",
        "partition": row.partition or "",
        "qos": row.qos or "",
        "job_name": row.job_name,
        "state": row.state,
        "cancelled_by": row.cancelled_by,
        "exit_code": row.exit_code,
        "signal": row.signal,
        "submit_time": row.submit,
        "start_time": row.start,
        "end_time": row.end,
        "end_day": end_local.date(),
        "elapsed_s": row.elapsed_s,
        "timelimit_s": row.timelimit_s,
        "wait_s": row.wait_s,
        "alloc_cpus": row.alloc_cpus,
        "req_cpus": row.req_cpus,
        "nnodes": row.nnodes,
        "node_list": row.node_list,
        "alloc_mem_mb": row.alloc_mem_mb,
        "req_mem_mb": row.req_mem_mb,
        "max_rss_mb": row.max_rss_mb,
        "mem_eff_approx": row.mem_eff_approx,
        "total_cpu_s": row.total_cpu_s,
        "cpu_seconds_alloc": row.cpu_seconds_alloc,
        "gpus": row.gpus,
        "gpu_type": row.gpu_type,
        "billing": row.billing,
        "ntasks": row.ntasks,
        "alloc_tres": row.alloc_tres,
        "req_tres": row.req_tres,
        "reason": row.reason,
        "ingest_batch_id": batch_id,
    }


UpsertResult = tuple[int, int, int, set[date], datetime | None, datetime | None]


def upsert_jobs(db: Session, records: list[dict], batch_size: int = 1000) -> UpsertResult:
    """Insert-or-update job records.

    Returns (inserted, updated, unchanged, end_days, min_start, max_end). min_start/max_end span
    the jobs that count for utilization (ran for >0 s with a consistent start), so one row with a
    bogus early start cannot widen the per-day utilization recompute to hundreds of days.
    """
    inserted = updated = unchanged = 0
    end_days: set[date] = set()
    min_start: datetime | None = None
    max_end: datetime | None = None

    # PostgreSQL caps a statement at 65535 bind parameters; ~45 columns => keep batches ~1000.
    batch_size = max(1, min(batch_size, 65535 // (len(Job.__table__.columns) + 1)))

    for i in range(0, len(records), batch_size):
        batch = records[i:i + batch_size]
        stmt = pg_insert(Job).values(batch)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_jobs_cluster_jobid",
            set_={**{c: getattr(stmt.excluded, c) for c in _JOB_UPDATE_COLS}, "updated_at": func.now()},
            where=tuple_(*(Job.__table__.c[c] for c in _JOB_COMPARE_COLS)).is_distinct_from(
                tuple_(*(getattr(stmt.excluded, c) for c in _JOB_COMPARE_COLS))),
        ).returning(literal_column("(xmax = 0)").label("inserted"))
        returned = 0
        for (was_insert,) in db.execute(stmt):
            returned += 1
            if was_insert:
                inserted += 1
            else:
                updated += 1
        unchanged += len(batch) - returned  # conflicts filtered out by the WHERE return no row
        for r in batch:
            end_days.add(r["end_day"])
            if max_end is None or r["end_time"] > max_end:
                max_end = r["end_time"]
            st = r["start_time"]
            if st is not None and r["elapsed_s"] > 0 and st <= r["end_time"] and (min_start is None or st < min_start):
                min_start = st
    return inserted, updated, unchanged, end_days, min_start, max_end


def insert_nodes(db: Session, cluster_id: int, taken_at: datetime, rows: list[NodeRow]) -> int:
    recs = [{
        "cluster_id": cluster_id, "taken_at": taken_at, "node_name": r.node_name, "state": r.state or "",
        "cpus_total": r.cpus_total or 0, "cpus_alloc": r.cpus_alloc or 0,
        "mem_mb_total": r.mem_mb_total or 0, "mem_mb_alloc": r.mem_mb_alloc or 0,
        "gpus_total": r.gpus_total or 0, "gpus_alloc": r.gpus_alloc or 0, "gpu_type": r.gpu_type,
        "partitions": r.partitions, "features": r.features,
    } for r in rows]
    if recs:
        db.execute(pg_insert(NodeSnapshot), recs)
    return len(recs)


def insert_partitions(db: Session, cluster_id: int, taken_at: datetime, rows: list[PartitionRow]) -> int:
    recs = [{
        "cluster_id": cluster_id, "taken_at": taken_at, "partition": r.partition, "state": r.state or "",
        "total_cpus": r.total_cpus, "total_nodes": r.total_nodes, "max_time_s": r.max_time_s,
        "default_time_s": r.default_time_s, "nodes": r.nodes,
    } for r in rows]
    if recs:
        db.execute(pg_insert(PartitionSnapshot), recs)
    return len(recs)


def insert_fairshare(db: Session, cluster_id: int, taken_at: datetime, rows: list[FairshareRow]) -> int:
    recs = [{
        "cluster_id": cluster_id, "taken_at": taken_at, "account": r.account, "user_name": r.user,
        "depth": r.depth, "raw_shares": r.raw_shares, "shares_parent": r.shares_parent,
        "norm_shares": r.norm_shares, "raw_usage": r.raw_usage, "norm_usage": r.norm_usage,
        "effective_usage": r.effective_usage, "fairshare": r.fairshare, "level_fs": r.level_fs,
    } for r in rows]
    if recs:
        db.execute(pg_insert(FairshareSnapshot), recs)
    return len(recs)
