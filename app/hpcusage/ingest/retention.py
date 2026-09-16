from sqlalchemy import text
from sqlalchemy.orm import Session


def purge_old_jobs(db: Session, cluster_id: int, retention_days: int) -> int:
    """Delete per-job rows older than the retention window. Rollups are kept forever."""
    if retention_days <= 0:
        return 0
    res = db.execute(
        text("DELETE FROM jobs WHERE cluster_id = :cid AND end_time < now() - make_interval(days => :days)"),
        {"cid": cluster_id, "days": retention_days},
    )
    return res.rowcount


def purge_old_snapshots(db: Session, cluster_id: int, retention_days: int) -> int:
    if retention_days <= 0:
        return 0
    total = 0
    for table in ("node_snapshots", "partition_snapshots"):
        res = db.execute(
            text(f"DELETE FROM {table} WHERE cluster_id = :cid AND taken_at < now() - make_interval(days => :days)"),
            {"cid": cluster_id, "days": retention_days},
        )
        total += res.rowcount
    return total
