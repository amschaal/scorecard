"""Fairshare, node state and ingest-status queries."""

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

from .common import fnum


def fairshare_latest(db: Session, cluster_id: int) -> dict:
    taken = db.execute(text("SELECT max(taken_at) FROM fairshare_snapshots WHERE cluster_id = :cid"),
                       {"cid": cluster_id}).scalar()
    if taken is None:
        return {"taken_at": None, "rows": []}
    rows = db.execute(text("""
        SELECT account, user_name, depth, raw_shares, shares_parent, norm_shares, raw_usage, norm_usage,
               effective_usage, fairshare, level_fs
        FROM fairshare_snapshots WHERE cluster_id = :cid AND taken_at = :taken
        ORDER BY id
    """), {"cid": cluster_id, "taken": taken}).mappings().all()
    return {"taken_at": taken.isoformat(), "rows": [dict(r) for r in rows]}


def fairshare_series(db: Session, cluster_id: int, start: date, end: date, account: str,
                     user: str | None = None, tz: str = "UTC") -> dict:
    """`t` is ISO-8601 with offset (for API consumers); `t_local` is the same instant in the
    cluster's zone without an offset, which Plotly plots as-is instead of converting to UTC."""
    zone = ZoneInfo(tz)
    user_clause = "AND user_name = :user" if user else "AND user_name IS NULL"
    rows = db.execute(text(f"""
        SELECT taken_at, fairshare, norm_usage, effective_usage, norm_shares, level_fs
        FROM fairshare_snapshots
        WHERE cluster_id = :cid AND account = :account {user_clause}
          AND taken_at >= :start AND taken_at < :end
        ORDER BY taken_at
    """), {"cid": cluster_id, "account": account, "user": user, "start": start, "end": end}).mappings().all()
    return {
        "account": account, "user": user,
        "t": [r["taken_at"].isoformat() for r in rows],
        "t_local": [r["taken_at"].astimezone(zone).strftime("%Y-%m-%d %H:%M:%S") for r in rows],
        "fairshare": [r["fairshare"] for r in rows],
        "norm_usage": [r["norm_usage"] for r in rows],
        "effective_usage": [r["effective_usage"] for r in rows],
        "norm_shares": [r["norm_shares"] for r in rows],
        "level_fs": [r["level_fs"] for r in rows],
    }


def nodes_latest(db: Session, cluster_id: int) -> dict:
    taken = db.execute(text("SELECT max(taken_at) FROM node_snapshots WHERE cluster_id = :cid"),
                       {"cid": cluster_id}).scalar()
    if taken is None:
        return {"taken_at": None, "nodes": [], "by_state": {}, "by_partition": [], "totals": {}}
    rows = db.execute(text("""
        SELECT node_name, state, cpus_total, cpus_alloc, mem_mb_total, mem_mb_alloc, gpus_total, gpus_alloc,
               gpu_type, partitions, features
        FROM node_snapshots WHERE cluster_id = :cid AND taken_at = :taken ORDER BY node_name
    """), {"cid": cluster_id, "taken": taken}).mappings().all()
    by_state: dict[str, int] = defaultdict(int)
    by_part: dict[str, dict] = defaultdict(lambda: {"nodes": 0, "cpus_total": 0, "cpus_alloc": 0,
                                                    "gpus_total": 0, "gpus_alloc": 0, "mem_mb_total": 0,
                                                    "mem_mb_alloc": 0, "down": 0})
    totals = {"nodes": 0, "cpus_total": 0, "cpus_alloc": 0, "gpus_total": 0, "gpus_alloc": 0,
              "mem_mb_total": 0, "mem_mb_alloc": 0, "down": 0}
    nodes = []
    for r in rows:
        d = dict(r)
        base_state = d["state"].split("+")[0].split("*")[0] or "UNKNOWN"
        d["base_state"] = base_state
        unavailable = ("DOWN" in d["state"]) or ("DRAIN" in d["state"]) or ("FUTURE" in d["state"])
        by_state[d["state"]] += 1
        nodes.append(d)
        for bucket in [totals] + [by_part[p] for p in d["partitions"]]:
            bucket["nodes"] += 1
            bucket["down"] += 1 if unavailable else 0
            for k in ("cpus_total", "cpus_alloc", "gpus_total", "gpus_alloc", "mem_mb_total", "mem_mb_alloc"):
                bucket[k] += int(d[k] or 0)
    by_partition = [{"partition": p, **v} for p, v in sorted(by_part.items())]
    return {"taken_at": taken.isoformat(), "nodes": nodes, "by_state": dict(by_state),
            "by_partition": by_partition, "totals": totals}


def partitions_latest(db: Session, cluster_id: int) -> list[dict]:
    taken = db.execute(text("SELECT max(taken_at) FROM partition_snapshots WHERE cluster_id = :cid"),
                       {"cid": cluster_id}).scalar()
    if taken is None:
        return []
    rows = db.execute(text("""
        SELECT partition, state, total_cpus, total_nodes, max_time_s, default_time_s, nodes
        FROM partition_snapshots WHERE cluster_id = :cid AND taken_at = :taken ORDER BY partition
    """), {"cid": cluster_id, "taken": taken}).mappings().all()
    return [dict(r) for r in rows]


def ingest_status(db: Session, stale_after_hours: int = 36) -> list[dict]:
    rows = db.execute(text("""
        SELECT DISTINCT ON (c.name, b.kind)
               c.name AS cluster, c.timezone, b.kind, b.id, b.received_at, b.window_start, b.window_end, b.taken_at,
               b.rows_received, b.rows_inserted, b.rows_updated, b.status, b.error, b.duration_ms,
               b.collector_version, b.slurm_version
        FROM ingest_batches b JOIN clusters c ON c.id = b.cluster_id
        ORDER BY c.name, b.kind, b.received_at DESC
    """)).mappings().all()
    now = datetime.now(timezone.utc)
    out = []
    for r in rows:
        d = dict(r)
        age = now - r["received_at"]
        d["age_hours"] = age.total_seconds() / 3600.0
        d["stale"] = age > timedelta(hours=stale_after_hours) or r["status"] != "ok"
        out.append(d)
    return out


def recent_batches(db: Session, limit: int = 50) -> list[dict]:
    rows = db.execute(text("""
        SELECT c.name AS cluster, c.timezone, b.kind, b.id, b.received_at, b.window_start, b.window_end,
               b.rows_received, b.rows_inserted, b.rows_updated, b.status, b.error, b.duration_ms
        FROM ingest_batches b JOIN clusters c ON c.id = b.cluster_id
        ORDER BY b.received_at DESC LIMIT :limit
    """), {"limit": limit}).mappings().all()
    return [dict(r) for r in rows]


def cluster_overview(db: Session, cluster_id: int, start: date, end: date) -> dict:
    """Overview-card numbers for one cluster."""
    row = db.execute(text("""
        SELECT sum(cpu_seconds_alloc) / 3600.0 AS cpu_hours, sum(gpu_seconds) / 3600.0 AS gpu_hours,
               sum(job_count) AS jobs, count(DISTINCT user_name) AS users, max(day) AS last_day
        FROM daily_usage WHERE cluster_id = :cid AND day >= :start AND day < :end
    """), {"cid": cluster_id, "start": start, "end": end}).mappings().one()
    spark = db.execute(text("""
        SELECT day, sum(cpu_seconds_alloc) / 3600.0 AS cpu_hours
        FROM daily_usage WHERE cluster_id = :cid AND day >= :start AND day < :end GROUP BY day ORDER BY day
    """), {"cid": cluster_id, "start": start, "end": end}).all()
    return {"cpu_hours": fnum(row["cpu_hours"]), "gpu_hours": fnum(row["gpu_hours"]),
            "jobs": int(row["jobs"] or 0), "users": int(row["users"] or 0),
            "last_day": row["last_day"].isoformat() if row["last_day"] else None,
            "spark": {"x": [d.isoformat() for d, _ in spark], "y": [fnum(v) for _, v in spark]}}
