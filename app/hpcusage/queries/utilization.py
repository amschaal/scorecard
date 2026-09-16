"""Queries over daily_partition_util (utilization vs capacity, queue wait) and job-level wait histograms."""

from collections import defaultdict
from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

from .common import fnum

RESOURCES = {
    "cpu": ("cpu_seconds", "capacity_cpu_seconds", "CPU"),
    "gpu": ("gpu_seconds", "capacity_gpu_seconds", "GPU"),
    "mem": ("mem_mb_seconds", "capacity_mem_mb_seconds", "Memory"),
    "node": ("node_seconds", "capacity_node_seconds", "Node"),
}

WAIT_BUCKETS = [
    ("< 1 min", 0, 60), ("1–10 min", 60, 600), ("10–60 min", 600, 3600),
    ("1–6 h", 3600, 21600), ("6–24 h", 21600, 86400), ("> 1 day", 86400, None),
]


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
    rows = db.execute(text(f"""
        SELECT day, partition, wait_p50_s, wait_p90_s, wait_mean_s, jobs_started
        FROM daily_partition_util
        WHERE cluster_id = :cid AND day >= :start AND day < :end AND jobs_started > 0 {where}
        ORDER BY day, partition
    """), {"cid": cluster_id, "start": start, "end": end, "partition": partition}).mappings().all()
    days = sorted({r["day"] for r in rows})
    per: dict[str, dict] = defaultdict(lambda: {"p50": {}, "p90": {}, "mean": {}, "jobs": 0})
    for r in rows:
        p = per[r["partition"]]
        p["p50"][r["day"]] = fnum(r["wait_p50_s"]) / 3600.0 if r["wait_p50_s"] is not None else None
        p["p90"][r["day"]] = fnum(r["wait_p90_s"]) / 3600.0 if r["wait_p90_s"] is not None else None
        p["mean"][r["day"]] = fnum(r["wait_mean_s"]) / 3600.0 if r["wait_mean_s"] is not None else None
        p["jobs"] += int(r["jobs_started"] or 0)
    parts = sorted(per, key=lambda k: per[k]["jobs"], reverse=True)
    series = [{"name": k, "jobs_started": per[k]["jobs"],
               "p50": [per[k]["p50"].get(d) for d in days],
               "p90": [per[k]["p90"].get(d) for d in days],
               "mean": [per[k]["mean"].get(d) for d in days]} for k in parts]

    # Histogram from per-job rows (only available inside the retention window).
    cases = " ".join(
        f"WHEN wait_s >= {lo} {'AND wait_s < ' + str(hi) if hi else ''} THEN {i}"
        for i, (_, lo, hi) in enumerate(WAIT_BUCKETS)
    )
    hist_rows = db.execute(text(f"""
        SELECT partition, CASE {cases} END AS bucket, count(*) AS n
        FROM jobs
        WHERE cluster_id = :cid AND end_day >= :start AND end_day < :end AND wait_s IS NOT NULL {where}
        GROUP BY 1, 2
    """), {"cid": cluster_id, "start": start, "end": end, "partition": partition}).mappings().all()
    hist: dict[str, list[int]] = defaultdict(lambda: [0] * len(WAIT_BUCKETS))
    for r in hist_rows:
        if r["bucket"] is not None:
            hist[r["partition"]][int(r["bucket"])] += int(r["n"])
    histogram = [{"name": p, "values": hist[p]} for p in sorted(hist, key=lambda p: -sum(hist[p]))]
    return {"unit": "hours", "days": [d.isoformat() for d in days], "series": series,
            "histogram": {"buckets": [b[0] for b in WAIT_BUCKETS], "series": histogram}}
