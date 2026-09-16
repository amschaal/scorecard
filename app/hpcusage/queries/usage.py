"""Queries over daily_usage: summaries, time series, leaderboards, outcomes, efficiency."""

from collections import defaultdict
from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

from .common import build_filters, check_interval, fnum, group_col, metric_expr

OUTCOME_COLS = ["jobs_completed", "jobs_failed", "jobs_timeout", "jobs_cancelled", "jobs_oom",
                "jobs_node_fail", "jobs_preempted"]
OUTCOME_LABELS = {
    "jobs_completed": "Completed", "jobs_failed": "Failed", "jobs_timeout": "Timeout",
    "jobs_cancelled": "Cancelled", "jobs_oom": "Out of memory", "jobs_node_fail": "Node failure",
    "jobs_preempted": "Preempted",
}


def summary(db: Session, cluster_id: int, start: date, end: date, **filters) -> dict:
    where, params = build_filters(filters)
    row = db.execute(text(f"""
        SELECT sum(cpu_seconds_alloc) / 3600.0 AS cpu_hours,
               sum(gpu_seconds) / 3600.0 AS gpu_hours,
               sum(mem_mb_seconds) / 1024.0 / 3600.0 AS mem_gb_hours,
               sum(node_seconds) / 3600.0 AS node_hours,
               sum(job_count) AS jobs,
               count(DISTINCT user_name) AS users,
               count(DISTINCT account) AS accounts,
               sum(jobs_completed) AS completed, sum(jobs_failed) AS failed, sum(jobs_timeout) AS timeout,
               sum(jobs_cancelled) AS cancelled, sum(jobs_oom) AS oom,
               sum(wait_seconds_sum) / nullif(sum(jobs_with_wait), 0) AS mean_wait_s,
               sum(cpu_seconds_used) / nullif(sum(cpu_seconds_alloc), 0) AS cpu_eff,
               sum(max_rss_mb_sum)::float / nullif(sum(alloc_mem_mb_sum), 0) AS mem_eff,
               sum(elapsed_limited_sum)::float / nullif(sum(timelimit_s_sum), 0) AS time_eff,
               sum(jobs_under_5min) AS short_jobs
        FROM daily_usage
        WHERE cluster_id = :cid AND day >= :start AND day < :end {where}
    """), {"cid": cluster_id, "start": start, "end": end, **params}).mappings().one()
    out = {k: (fnum(v) if k not in ("jobs", "users", "accounts", "completed", "failed", "timeout",
                                    "cancelled", "oom", "short_jobs") else int(v or 0))
           for k, v in row.items()}
    out["success_rate"] = out["completed"] / out["jobs"] if out["jobs"] else None
    for k in ("cpu_eff", "mem_eff", "time_eff", "mean_wait_s"):
        if row[k] is None:
            out[k] = None
    return out


def timeseries(db: Session, cluster_id: int, start: date, end: date, metric: str = "cpu_hours",
               group_by: str | None = None, interval: str = "day", top_n: int = 10, **filters) -> dict:
    expr, label, unit = metric_expr(metric)
    gcol = group_col(group_by)
    check_interval(interval)
    where, params = build_filters(filters)
    gsel = f"{gcol} AS grp" if gcol else "'total' AS grp"
    group_sql = "GROUP BY 1, 2 ORDER BY 1, 2" if gcol else "GROUP BY 1 ORDER BY 1"
    rows = db.execute(text(f"""
        SELECT date_trunc('{interval}', day)::date AS bucket, {gsel}, {expr} AS value
        FROM daily_usage
        WHERE cluster_id = :cid AND day >= :start AND day < :end {where}
        {group_sql}
    """), {"cid": cluster_id, "start": start, "end": end, **params}).all()

    buckets = sorted({r.bucket for r in rows})
    per_group: dict[str, dict[date, float]] = defaultdict(dict)
    totals: dict[str, float] = defaultdict(float)
    for r in rows:
        per_group[r.grp][r.bucket] = fnum(r.value)
        totals[r.grp] += fnum(r.value)
    ranked = sorted(totals, key=lambda g: totals[g], reverse=True)
    keep, rest = ranked[:top_n], ranked[top_n:]
    series = [{"name": g, "total": totals[g], "values": [per_group[g].get(b, 0.0) for b in buckets]}
              for g in keep]
    if rest:
        other = [sum(per_group[g].get(b, 0.0) for g in rest) for b in buckets]
        series.append({"name": "other", "total": sum(totals[g] for g in rest), "values": other,
                       "collapsed": len(rest)})
    return {"metric": metric, "label": label, "unit": unit, "interval": interval, "group_by": group_by or "none",
            "buckets": [b.isoformat() for b in buckets], "series": series}


def leaderboard(db: Session, cluster_id: int, start: date, end: date, metric: str = "cpu_hours",
                by: str = "user", limit: int = 25, **filters) -> dict:
    expr, label, unit = metric_expr(metric)
    gcol = group_col(by)
    if gcol is None:
        gcol = "user_name"
    where, params = build_filters(filters)
    base = f"FROM daily_usage WHERE cluster_id = :cid AND day >= :start AND day < :end {where}"
    p = {"cid": cluster_id, "start": start, "end": end, **params}
    total = fnum(db.execute(text(f"SELECT {expr} {base}"), p).scalar())
    rows = db.execute(text(f"""
        SELECT {gcol} AS name, {expr} AS value,
               sum(job_count) AS jobs,
               sum(cpu_seconds_alloc) / 3600.0 AS cpu_hours,
               sum(gpu_seconds) / 3600.0 AS gpu_hours,
               sum(mem_mb_seconds) / 1024.0 / 3600.0 AS mem_gb_hours,
               sum(cpu_seconds_used) / nullif(sum(cpu_seconds_alloc), 0) AS cpu_eff,
               count(DISTINCT account) AS accounts,
               string_agg(DISTINCT account, ', ') AS account_list
        {base}
        GROUP BY 1 ORDER BY 2 DESC NULLS LAST LIMIT :limit
    """), {**p, "limit": limit}).mappings().all()
    items = []
    for i, r in enumerate(rows, 1):
        items.append({
            "rank": i, "name": r["name"], "value": fnum(r["value"]), "jobs": int(r["jobs"] or 0),
            "cpu_hours": fnum(r["cpu_hours"]), "gpu_hours": fnum(r["gpu_hours"]),
            "mem_gb_hours": fnum(r["mem_gb_hours"]),
            "cpu_eff": fnum(r["cpu_eff"]) if r["cpu_eff"] is not None else None,
            "share": (fnum(r["value"]) / total) if total else 0.0,
            "accounts": r["account_list"] if by == "user" else None,
        })
    return {"metric": metric, "label": label, "unit": unit, "by": by, "total": total, "items": items}


def outcomes(db: Session, cluster_id: int, start: date, end: date, interval: str = "day", **filters) -> dict:
    check_interval(interval)
    where, params = build_filters(filters)
    cols = ", ".join(f"sum({c}) AS {c}" for c in OUTCOME_COLS)
    rows = db.execute(text(f"""
        SELECT date_trunc('{interval}', day)::date AS bucket, {cols}, sum(job_count) AS total
        FROM daily_usage
        WHERE cluster_id = :cid AND day >= :start AND day < :end {where}
        GROUP BY 1 ORDER BY 1
    """), {"cid": cluster_id, "start": start, "end": end, **params}).mappings().all()
    buckets = [r["bucket"].isoformat() for r in rows]
    series = [{"name": OUTCOME_LABELS[c], "key": c, "values": [int(r[c] or 0) for r in rows]} for c in OUTCOME_COLS]
    totals = {OUTCOME_LABELS[c]: sum(int(r[c] or 0) for r in rows) for c in OUTCOME_COLS}
    return {"interval": interval, "buckets": buckets, "series": series, "totals": totals}


def efficiency(db: Session, cluster_id: int, start: date, end: date, by: str = "user",
               min_cpu_hours: float = 100.0, limit: int = 100, sort: str = "wasted_cpu_hours", **filters) -> dict:
    """The 'naughty list': who allocates a lot and uses little."""
    gcol = group_col(by) or "user_name"
    where, params = build_filters(filters)
    sort_cols = {"wasted_cpu_hours", "cpu_hours", "cpu_eff", "mem_eff", "time_eff", "timeout_rate",
                 "wasted_mem_gb_hours"}
    if sort not in sort_cols:
        sort = "wasted_cpu_hours"
    rows = db.execute(text(f"""
        WITH agg AS (
            SELECT {gcol} AS name,
                   sum(job_count) AS jobs,
                   sum(cpu_seconds_alloc) / 3600.0 AS cpu_hours,
                   sum(gpu_seconds) / 3600.0 AS gpu_hours,
                   sum(mem_mb_seconds) / 1024.0 / 3600.0 AS mem_gb_hours,
                   sum(cpu_seconds_used) / nullif(sum(cpu_seconds_alloc), 0) AS cpu_eff,
                   sum(max_rss_mb_sum)::float / nullif(sum(alloc_mem_mb_sum), 0) AS mem_eff,
                   sum(elapsed_limited_sum)::float / nullif(sum(timelimit_s_sum), 0) AS time_eff,
                   sum(jobs_timeout)::float / nullif(sum(job_count), 0) AS timeout_rate,
                   sum(jobs_oom)::float / nullif(sum(job_count), 0) AS oom_rate,
                   sum(jobs_under_5min)::float / nullif(sum(job_count), 0) AS short_job_rate,
                   sum(jobs_with_rss) AS jobs_with_rss
            FROM daily_usage
            WHERE cluster_id = :cid AND day >= :start AND day < :end {where}
            GROUP BY 1
        )
        SELECT *,
               cpu_hours * (1 - coalesce(cpu_eff, 1)) AS wasted_cpu_hours,
               mem_gb_hours * (1 - coalesce(mem_eff, 1)) AS wasted_mem_gb_hours
        FROM agg
        WHERE cpu_hours >= :min_cpu_hours
        ORDER BY {sort} DESC NULLS LAST
        LIMIT :limit
    """), {"cid": cluster_id, "start": start, "end": end, "min_cpu_hours": min_cpu_hours, "limit": limit,
           **params}).mappings().all()
    items = []
    for r in rows:
        d = {k: (fnum(v) if v is not None else None) for k, v in r.items() if k != "name"}
        d["name"] = r["name"]
        d["jobs"] = int(r["jobs"] or 0)
        d["jobs_with_rss"] = int(r["jobs_with_rss"] or 0)
        items.append(d)
    return {"by": by, "min_cpu_hours": min_cpu_hours, "sort": sort, "items": items}


def top_entities(db: Session, cluster_id: int, start: date, end: date, col: str, limit: int = 10,
                 **filters) -> list[dict]:
    """Small helper for page sidebars: top N of a column by CPU-hours."""
    where, params = build_filters(filters)
    rows = db.execute(text(f"""
        SELECT {col} AS name, sum(cpu_seconds_alloc) / 3600.0 AS cpu_hours, sum(job_count) AS jobs
        FROM daily_usage WHERE cluster_id = :cid AND day >= :start AND day < :end {where}
        GROUP BY 1 ORDER BY 2 DESC LIMIT :limit
    """), {"cid": cluster_id, "start": start, "end": end, "limit": limit, **params}).mappings().all()
    return [{"name": r["name"], "cpu_hours": fnum(r["cpu_hours"]), "jobs": int(r["jobs"] or 0)} for r in rows]
