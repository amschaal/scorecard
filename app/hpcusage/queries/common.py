"""Shared helpers for metric queries against the rollup tables."""

from datetime import date, timedelta

from fastapi import HTTPException

# metric key -> (SQL expression over daily_usage, human label, unit)
METRICS: dict[str, tuple[str, str, str]] = {
    "cpu_hours": ("sum(cpu_seconds_alloc) / 3600.0", "CPU-hours", "h"),
    "gpu_hours": ("sum(gpu_seconds) / 3600.0", "GPU-hours", "h"),
    "mem_gb_hours": ("sum(mem_mb_seconds) / 1024.0 / 3600.0", "Memory GB-hours", "GB·h"),
    "node_hours": ("sum(node_seconds) / 3600.0", "Node-hours", "h"),
    "wall_hours": ("sum(wall_seconds) / 3600.0", "Wall-clock hours", "h"),
    "billing_hours": ("sum(billing_seconds) / 3600.0", "Billing (TRES) hours", "h"),
    "jobs": ("sum(job_count)", "Jobs", ""),
}

GROUP_COLUMNS: dict[str, str] = {
    "user": "user_name",
    "account": "account",
    "partition": "partition",
    "qos": "qos",
}

INTERVALS = ("day", "week", "month")

# Filters accepted by most endpoints: query-param name -> daily_usage column
FILTER_COLUMNS: dict[str, str] = {"user": "user_name", "account": "account", "partition": "partition", "qos": "qos"}


def metric_expr(metric: str) -> tuple[str, str, str]:
    if metric not in METRICS:
        raise HTTPException(400, f"unknown metric {metric!r}; one of {', '.join(METRICS)}")
    return METRICS[metric]


def group_col(group_by: str | None) -> str | None:
    if not group_by or group_by == "none":
        return None
    if group_by not in GROUP_COLUMNS:
        raise HTTPException(400, f"unknown group_by {group_by!r}; one of {', '.join(GROUP_COLUMNS)}")
    return GROUP_COLUMNS[group_by]


def check_interval(interval: str) -> str:
    if interval not in INTERVALS:
        raise HTTPException(400, f"unknown interval {interval!r}")
    return interval


def parse_window(from_: str | None, to: str | None, default_days: int = 30) -> tuple[date, date]:
    """Return (start, end_exclusive). 'to' is inclusive in the URL for humans."""
    try:
        end_incl = date.fromisoformat(to) if to else date.today()
        start = date.fromisoformat(from_) if from_ else end_incl - timedelta(days=default_days - 1)
    except ValueError as e:
        raise HTTPException(400, f"bad date: {e}") from e
    if start > end_incl:
        raise HTTPException(400, "from must be <= to")
    if (end_incl - start).days > 5 * 366:
        raise HTTPException(400, "window too large (max 5 years)")
    return start, end_incl + timedelta(days=1)


def build_filters(filters: dict[str, str | None]) -> tuple[str, dict]:
    """Turn {"user": "alice", "partition": None} into SQL 'AND user_name = :f_user ...' + params."""
    clauses, params = [], {}
    for key, value in filters.items():
        if value:
            col = FILTER_COLUMNS[key]
            clauses.append(f"AND {col} = :f_{key}")
            params[f"f_{key}"] = value
    return " ".join(clauses), params


def fnum(v) -> float:
    return float(v) if v is not None else 0.0
