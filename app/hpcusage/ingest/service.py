"""Orchestrates one collector envelope: audit row, validation, upsert, rollups, retention."""

import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Cluster, IngestBatch
from ..settings import Settings
from . import retention, rollup, upsert
from .schemas import Envelope, FairshareRow, JobRow, NodeRow, PartitionRow


def get_or_create_cluster(db: Session, name: str, settings: Settings, default_tz: str | None = None) -> Cluster:
    cluster = db.scalar(select(Cluster).where(Cluster.name == name))
    if cluster is None:
        cfg = settings.cluster_config.get(name, {})
        cluster = Cluster(
            name=name,
            display_name=cfg.get("display_name", name.capitalize()),
            timezone=cfg.get("timezone") or default_tz or "America/Los_Angeles",
        )
        db.add(cluster)
        db.flush()
    return cluster


def _days_between(start: datetime, end: datetime, tz: ZoneInfo) -> set[date]:
    d = start.astimezone(tz).date()
    last = end.astimezone(tz).date()
    out = set()
    while d <= last:
        out.add(d)
        d += timedelta(days=1)
    return out


def ingest_envelope(db: Session, env: Envelope, settings: Settings) -> dict:
    t0 = time.monotonic()
    cluster = get_or_create_cluster(db, env.cluster, settings, env.timezone)
    tz = ZoneInfo(cluster.timezone)
    taken_at = env.taken_at or datetime.now(timezone.utc)

    batch = IngestBatch(
        cluster_id=cluster.id, kind=env.kind, window_start=env.window_start, window_end=env.window_end,
        taken_at=taken_at, rows_received=len(env.rows), collector_version=env.collector_version,
        slurm_version=env.slurm_version, status="running",
    )
    db.add(batch)
    db.flush()

    result: dict = {"batch_id": batch.id, "cluster": cluster.name, "kind": env.kind, "rows_received": len(env.rows)}
    try:
        if env.kind == "jobs":
            rows = [JobRow.model_validate(r) for r in env.rows]
            records = [upsert.job_row_to_record(r, cluster.id, tz, batch.id) for r in rows]
            inserted, updated, end_days, min_start, max_end = upsert.upsert_jobs(
                db, records, settings.upsert_batch_size
            )
            usage_rows = rollup.recompute_daily_usage(db, cluster.id, end_days)
            util_days = _days_between(min_start, max_end, tz) if (min_start and max_end) else set()
            util_rows = rollup.recompute_partition_util(db, cluster.id, cluster.timezone, util_days)
            purged = retention.purge_old_jobs(db, cluster.id, settings.job_retention_days)
            batch.rows_inserted, batch.rows_updated = inserted, updated
            result.update(inserted=inserted, updated=updated, rollup_days=sorted(str(d) for d in end_days),
                          util_days=len(util_days), daily_usage_rows=usage_rows,
                          partition_util_rows=util_rows, purged_jobs=purged)
        elif env.kind == "nodes":
            n = upsert.insert_nodes(db, cluster.id, taken_at, [NodeRow.model_validate(r) for r in env.rows])
            p = upsert.insert_partitions(db, cluster.id, taken_at,
                                         [PartitionRow.model_validate(r) for r in (env.partitions or [])])
            # A fresh capacity snapshot changes utilization for the day it describes.
            day = taken_at.astimezone(tz).date()
            util_rows = rollup.recompute_partition_util(db, cluster.id, cluster.timezone,
                                                        {day, day - timedelta(days=1)})
            purged = retention.purge_old_snapshots(db, cluster.id, settings.node_snapshot_retention_days)
            batch.rows_inserted = n + p
            result.update(inserted=n, partitions_inserted=p, partition_util_rows=util_rows, purged_snapshots=purged)
        elif env.kind == "fairshare":
            n = upsert.insert_fairshare(db, cluster.id, taken_at, [FairshareRow.model_validate(r) for r in env.rows])
            batch.rows_inserted = n
            result.update(inserted=n)
        batch.status = "ok"
    except Exception as e:  # noqa: BLE001 — recorded on the batch row, then re-raised
        # The flushed batch row was rolled back with everything else; record the failure on a fresh row.
        db.rollback()
        cluster_id = cluster.id
        db.add(IngestBatch(cluster_id=cluster_id, kind=env.kind, window_start=env.window_start,
                           window_end=env.window_end, taken_at=taken_at, rows_received=len(env.rows),
                           collector_version=env.collector_version, slurm_version=env.slurm_version,
                           status="error", error=str(e)[:2000]))
        db.commit()
        raise
    batch.duration_ms = int((time.monotonic() - t0) * 1000)
    db.commit()
    result["duration_ms"] = batch.duration_ms
    return result
