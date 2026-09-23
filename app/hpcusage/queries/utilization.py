"""Queries over daily_partition_util (utilization vs capacity, queue wait, wait histogram)."""

from collections import defaultdict
from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..models import WAIT_BUCKETS
from .common import fnum

RESOURCES = {
    "cpu": ("cpu_seconds", "capacity_cpu_seconds", "CPU"),
    "gpu": ("gpu_seconds", "capacity_gpu_seconds", "GPU"),
    "mem": ("mem_mb_seconds", "capacity_mem_mb_seconds", "Memory"),
    "node": ("node_seconds", "capacity_node_seconds", "Node"),
}


def utilization(db: Session, cluster_id: int, start: date, end: date, resource: str = "cpu",
                partition: str | None = None) -> dict:
    used_col, cap_col, label = RESOURCES.get(resource, RESOURCES["cpu"])
    where = "AND partition = :partition" if partition else ""
    rows = db.execute(text(f"""
        SELECT day, partition, {used_col} AS used, {cap_col} AS cap
        FROM daily_partition_util
        WHERE cluster_id = :cid AND day >= :start AND day < :end {where}
        ORDER BY day, partition
    """), {"cid": cluster_id, "start": start, "end": end, "partition": partition}).mappings().all()
    days = sorted({r["day"] for r in rows})
    per_part: dict[str, dict[date, float | None]] = defaultdict(dict)
    used_tot: dict[str, float] = defaultdict(float)
    cap_tot: dict[str, float] = defaultdict(float)
    for r in rows:
        used, cap = fnum(r["used"]), r["cap"]
        pct = (100.0 * used / fnum(cap)) if cap else None
        per_part[r["partition"]][r["day"]] = pct
        used_tot[r["partition"]] += used
        if cap:
            cap_tot[r["partition"]] += fnum(cap)
    parts = sorted(per_part, key=lambda p: cap_tot.get(p, 0), reverse=True)
    series = [{"name": p, "values": [per_part[p].get(d) for d in days],
               "avg_pct": (100.0 * used_tot[p] / cap_tot[p]) if cap_tot.get(p) else None,
               "used_hours": used_tot[p] / 3600.0, "capacity_hours": cap_tot[p] / 3600.0} for p in parts]
    return {"resource": resource, "label": label, "days": [d.isoformat() for d in days], "series": series}


def wait_times(db: Session, cluster_id: int, start: date, end: date, partition: str | None = None) -> dict:
    where = "AND partition = :partition" if partition else ""
    bucket_cols = ", ".join(c for c, *_ in WAIT_BUCKETS)
    rows = db.execute(text(f"""
        SELECT day, partition, wait_p50_s, wait_p90_s, wait_mean_s, jobs_started, {bucket_cols}
        FROM daily_partition_util
        WHERE cluster_id = :cid AND day >= :start AND day < :end AND jobs_started > 0 {where}
        ORDER BY day, partition
    """), {"cid": cluster_id, "start": start, "end": end, "partition": partition}).mappings().all()
    days = sorted({r["day"] for r in rows})
    per: dict[str, dict] = defaultdict(lambda: {"p50": {}, "p90": {}, "mean": {}, "jobs": 0,
                                                "hist": [0] * len(WAIT_BUCKETS)})
    for r in rows:
        p = per[r["partition"]]
        p["p50"][r["day"]] = fnum(r["wait_p50_s"]) / 3600.0 if r["wait_p50_s"] is not None else None
        p["p90"][r["day"]] = fnum(r["wait_p90_s"]) / 3600.0 if r["wait_p90_s"] is not None else None
        p["mean"][r["day"]] = fnum(r["wait_mean_s"]) / 3600.0 if r["wait_mean_s"] is not None else None
        p["jobs"] += int(r["jobs_started"] or 0)
        for i, (col, *_) in enumerate(WAIT_BUCKETS):
            p["hist"][i] += int(r[col] or 0)
    parts = sorted(per, key=lambda k: per[k]["jobs"], reverse=True)
    series = [{"name": k, "jobs_started": per[k]["jobs"],
               "p50": [per[k]["p50"].get(d) for d in days],
               "p90": [per[k]["p90"].get(d) for d in days],
               "mean": [per[k]["mean"].get(d) for d in days]} for k in parts]
    histogram = [{"name": k, "values": per[k]["hist"]} for k in sorted(per, key=lambda k: -sum(per[k]["hist"]))
                 if sum(per[k]["hist"])]
    return {"unit": "hours", "days": [d.isoformat() for d in days], "series": series,
            "histogram": {"buckets": [b[1] for b in WAIT_BUCKETS], "series": histogram}}
