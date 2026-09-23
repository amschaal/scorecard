"""Per-job browsing (only within the retention window)."""

from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..settings import get_settings

SORTABLE = {"end_time", "start_time", "elapsed_s", "alloc_cpus", "cpu_seconds_alloc", "wait_s", "max_rss_mb", "gpus"}

# The collector only ships jobs in a terminal state (TERMINAL_STATES in collector/slurm_collector.py), so
# the filter dropdown is a constant rather than SELECT DISTINCT over every per-job row.
JOB_STATES = ["COMPLETED", "FAILED", "TIMEOUT", "CANCELLED", "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED",
              "DEADLINE", "BOOT_FAIL", "REVOKED"]


def list_jobs(db: Session, cluster_id: int, start: date, end: date, user: str | None = None,
              account: str | None = None, partition: str | None = None, state: str | None = None,
              qos: str | None = None, job_id: str | None = None, min_cpus: int | None = None,
              gpus_only: bool = False, sort: str = "end_time", desc: bool = True,
              limit: int = 100, offset: int = 0, count: bool = True) -> dict:
    """List matching jobs. `count=False` skips the total; pages that only show "the latest N" do not
    need it and the API reports total=None. When the filters are all rollup dimensions (user, account,
    partition, qos) the total comes from daily_usage instead of counting every matching per-job row."""
    clauses, params = [], {"cid": cluster_id, "start": start, "end": end}
    rollup_clauses = []
    for col, val in (("user_name", user), ("account", account), ("partition", partition),
                     ("state", state), ("qos", qos)):
        if val:
            clauses.append(f"AND {col} = :{col}")
            params[col] = val
            if col != "state":
                rollup_clauses.append(f"AND {col} = :{col}")
    rollup_countable = not (state or job_id or min_cpus or gpus_only)
    if job_id:
        clauses.append("AND (job_id = :job_id OR job_id_raw = :job_id OR array_job_id::text = :job_id)")
        params["job_id"] = job_id
    if min_cpus:
        clauses.append("AND alloc_cpus >= :min_cpus")
        params["min_cpus"] = min_cpus
    if gpus_only:
        clauses.append("AND coalesce(gpus, 0) > 0")
    where = " ".join(clauses)
    if sort not in SORTABLE:
        sort = "end_time"
    direction = "DESC" if desc else "ASC"
    if sort == "end_time":
        # Lead with end_day: ix_jobs_cluster_end_day_end_time delivers the unfiltered listing in this
        # order directly (a backward index walk that stops after LIMIT rows), and the
        # (cluster, <filter column>, end_day) indexes let Postgres incremental-sort within each day
        # instead of sorting every job the user/account ran in the window.
        order = f"end_day {direction}, end_time {direction}"
    else:
        order = f"{sort} {direction} NULLS LAST"
    base = f"FROM jobs WHERE cluster_id = :cid AND end_day >= :start AND end_day < :end {where}"
    if not count:
        total = None
    elif rollup_countable:
        total = _rollup_count(db, params, " ".join(rollup_clauses))
    else:
        total = db.execute(text(f"SELECT count(*) {base}"), params).scalar()
    rows = db.execute(text(f"""
        SELECT job_id, job_id_raw, user_name, account, partition, qos, job_name, state, exit_code,
               submit_time, start_time, end_time, elapsed_s, timelimit_s, wait_s, alloc_cpus, nnodes,
               alloc_mem_mb, req_mem_mb, max_rss_mb, mem_eff_approx, total_cpu_s, cpu_seconds_alloc,
               gpus, gpu_type, node_list, reason
        {base} ORDER BY {order} LIMIT :limit OFFSET :offset
    """), {**params, "limit": limit, "offset": offset}).mappings().all()
    items = []
    for r in rows:
        d = dict(r)
        has_cpu = r["total_cpu_s"] is not None and r["cpu_seconds_alloc"]
        d["cpu_eff"] = (float(r["total_cpu_s"]) / r["cpu_seconds_alloc"]) if has_cpu else None
        has_mem = r["max_rss_mb"] is not None and r["alloc_mem_mb"]
        d["mem_eff"] = (r["max_rss_mb"] / r["alloc_mem_mb"]) if has_mem else None
        d["time_eff"] = (r["elapsed_s"] / r["timelimit_s"]) if r["timelimit_s"] else None
        for k in ("submit_time", "start_time", "end_time"):
            d[k] = d[k].isoformat() if d[k] else None
        items.append(d)
    return {"total": int(total or 0) if count else None, "limit": limit, "offset": offset, "items": items}


def _rollup_count(db: Session, params: dict, where: str) -> int:
    """Jobs in the window per daily_usage, which counts exactly the per-job rows while they exist. Days
    older than the retention window are left out because their rows have been purged (the boundary is
    approximate by a day: purge goes by end_time, the rollup by the cluster-local end_day)."""
    retention = get_settings().job_retention_days
    start = params["start"]
    if retention > 0:
        start = max(start, date.today() - timedelta(days=retention))
    return db.execute(text(f"""
        SELECT coalesce(sum(job_count), 0) FROM daily_usage
        WHERE cluster_id = :cid AND day >= :start AND day < :end {where}
    """), {**params, "start": start}).scalar()


def distinct_values(db: Session, cluster_id: int, col: str) -> list[str]:
    """Values for filter dropdowns. States are a fixed set; the rest come from the rollup."""
    if col == "state":
        return list(JOB_STATES)
    if col in ("partition", "qos", "account"):
        sql = f"SELECT DISTINCT {col} FROM daily_usage WHERE cluster_id = :cid ORDER BY 1"
    else:
        return []
    rows = db.execute(text(sql), {"cid": cluster_id}).scalars().all()
    return [r for r in rows if r]
