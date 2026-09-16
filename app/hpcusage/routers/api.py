"""JSON API consumed by the pages' charts (and anything else). Requires a CAS session."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_cluster, list_clusters, require_user
from ..models import Cluster
from ..queries import jobs as jobs_q
from ..queries import snapshots as snap_q
from ..queries import usage as usage_q
from ..queries import utilization as util_q
from ..queries.common import parse_window

router = APIRouter(prefix="/api/v1", tags=["api"], dependencies=[Depends(require_user)])


def _window(from_: str | None = Query(None, alias="from"), to: str | None = None):
    return parse_window(from_, to)


def _filters(user: str | None = None, account: str | None = None, partition: str | None = None,
             qos: str | None = None) -> dict:
    return {"user": user, "account": account, "partition": partition, "qos": qos}


@router.get("/clusters")
def clusters(db: Session = Depends(get_db)):
    return [{"name": c.name, "display_name": c.display_name, "timezone": c.timezone} for c in list_clusters(db)]


@router.get("/summary")
def summary(cluster: Cluster = Depends(get_cluster), window=Depends(_window), f=Depends(_filters),
            db: Session = Depends(get_db)):
    return usage_q.summary(db, cluster.id, *window, **f)


@router.get("/usage/timeseries")
def usage_timeseries(cluster: Cluster = Depends(get_cluster), window=Depends(_window), f=Depends(_filters),
                     metric: str = "cpu_hours", group_by: str = "none", interval: str = "day",
                     top_n: int = Query(10, ge=1, le=50), db: Session = Depends(get_db)):
    return usage_q.timeseries(db, cluster.id, *window, metric=metric, group_by=group_by, interval=interval,
                              top_n=top_n, **f)


@router.get("/leaderboard")
def leaderboard(cluster: Cluster = Depends(get_cluster), window=Depends(_window), f=Depends(_filters),
                metric: str = "cpu_hours", by: str = "user", limit: int = Query(25, ge=1, le=500),
                db: Session = Depends(get_db)):
    return usage_q.leaderboard(db, cluster.id, *window, metric=metric, by=by, limit=limit, **f)


@router.get("/outcomes")
def outcomes(cluster: Cluster = Depends(get_cluster), window=Depends(_window), f=Depends(_filters),
             interval: str = "day", db: Session = Depends(get_db)):
    return usage_q.outcomes(db, cluster.id, *window, interval=interval, **f)


@router.get("/efficiency")
def efficiency(cluster: Cluster = Depends(get_cluster), window=Depends(_window), f=Depends(_filters),
               by: str = "user", min_cpu_hours: float = Query(100.0, ge=0), limit: int = Query(100, ge=1, le=1000),
               sort: str = "wasted_cpu_hours", db: Session = Depends(get_db)):
    return usage_q.efficiency(db, cluster.id, *window, by=by, min_cpu_hours=min_cpu_hours, limit=limit,
                              sort=sort, **f)


@router.get("/utilization")
def utilization(cluster: Cluster = Depends(get_cluster), window=Depends(_window), resource: str = "cpu",
                partition: str | None = None, db: Session = Depends(get_db)):
    return util_q.utilization(db, cluster.id, *window, resource=resource, partition=partition)


@router.get("/wait_times")
def wait_times(cluster: Cluster = Depends(get_cluster), window=Depends(_window), partition: str | None = None,
               db: Session = Depends(get_db)):
    return util_q.wait_times(db, cluster.id, *window, partition=partition)


@router.get("/fairshare")
def fairshare(cluster: Cluster = Depends(get_cluster), window=Depends(_window), account: str | None = None,
              user: str | None = None, db: Session = Depends(get_db)):
    if account:
        return snap_q.fairshare_series(db, cluster.id, *window, account=account, user=user)
    return snap_q.fairshare_latest(db, cluster.id)


@router.get("/nodes/latest")
def nodes_latest(cluster: Cluster = Depends(get_cluster), db: Session = Depends(get_db)):
    return snap_q.nodes_latest(db, cluster.id)


@router.get("/jobs")
def jobs(cluster: Cluster = Depends(get_cluster), window=Depends(_window), user: str | None = None,
         account: str | None = None, partition: str | None = None, state: str | None = None,
         qos: str | None = None, job_id: str | None = None, min_cpus: int | None = None,
         gpus_only: bool = False, sort: str = "end_time", desc: bool = True,
         limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    return jobs_q.list_jobs(db, cluster.id, *window, user=user, account=account, partition=partition,
                            state=state, qos=qos, job_id=job_id, min_cpus=min_cpus, gpus_only=gpus_only,
                            sort=sort, desc=desc, limit=limit, offset=offset)


@router.get("/ingest/status")
def ingest_status(db: Session = Depends(get_db)):
    return snap_q.ingest_status(db)
