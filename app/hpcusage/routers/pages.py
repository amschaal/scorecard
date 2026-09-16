"""Server-rendered pages. Each page gets the cluster list, the current window and any
entity it is about; charts are filled in by static/charts.js from the JSON API."""

from datetime import timedelta

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_cluster, list_clusters, require_user
from ..models import Cluster
from ..queries import jobs as jobs_q
from ..queries import snapshots as snap_q
from ..queries import usage as usage_q
from ..queries.common import METRICS, parse_window
from ..templating import templates

router = APIRouter(tags=["pages"], include_in_schema=False)


def ctx(request: Request, db: Session, user: str, cluster: Cluster | None = None, from_=None, to=None,
        default_days: int = 30, **extra) -> dict:
    start, end = parse_window(from_, to, default_days)
    return {
        "request": request,
        "user": user,
        "display_name": request.session.get("display_name") if hasattr(request, "session") else None,
        "clusters": list_clusters(db),
        "cluster": cluster,
        "window": {"from": start.isoformat(), "to": (end - timedelta(days=1)).isoformat(),
                   "days": (end - start).days},
        "metrics": METRICS,
        **extra,
    }


@router.get("/", response_class=HTMLResponse)
def index(request: Request, user: str = Depends(require_user), db: Session = Depends(get_db),
          from_: str | None = Query(None, alias="from"), to: str | None = None):
    c = ctx(request, db, user, None, from_, to)
    start, end = parse_window(from_, to)
    c["overview"] = {cl.name: snap_q.cluster_overview(db, cl.id, start, end) for cl in c["clusters"]}
    c["ingest"] = snap_q.ingest_status(db)
    return templates.TemplateResponse(request, "index.html", c)


@router.get("/c/{cluster}", response_class=HTMLResponse)
def cluster_page(request: Request, cluster: Cluster = Depends(get_cluster), user: str = Depends(require_user),
                 db: Session = Depends(get_db), from_: str | None = Query(None, alias="from"), to: str | None = None):
    c = ctx(request, db, user, cluster, from_, to)
    start, end = parse_window(from_, to)
    c["summary"] = usage_q.summary(db, cluster.id, start, end)
    c["partitions"] = jobs_q.distinct_values(db, cluster.id, "partition")
    return templates.TemplateResponse(request, "cluster.html", c)


@router.get("/c/{cluster}/leaderboard", response_class=HTMLResponse)
def leaderboard_page(request: Request, cluster: Cluster = Depends(get_cluster), user: str = Depends(require_user),
                     db: Session = Depends(get_db), from_: str | None = Query(None, alias="from"),
                     to: str | None = None, metric: str = "cpu_hours", by: str = "user",
                     partition: str | None = None, account: str | None = None):
    c = ctx(request, db, user, cluster, from_, to, metric=metric, by=by, partition=partition or "",
            account=account or "")
    c["partitions"] = jobs_q.distinct_values(db, cluster.id, "partition")
    return templates.TemplateResponse(request, "leaderboard.html", c)


@router.get("/c/{cluster}/users/{username}", response_class=HTMLResponse)
def user_page(request: Request, username: str, cluster: Cluster = Depends(get_cluster),
              user: str = Depends(require_user), db: Session = Depends(get_db),
              from_: str | None = Query(None, alias="from"), to: str | None = None):
    c = ctx(request, db, user, cluster, from_, to, entity_type="user", entity=username)
    start, end = parse_window(from_, to)
    c["summary"] = usage_q.summary(db, cluster.id, start, end, user=username)
    c["accounts"] = usage_q.top_entities(db, cluster.id, start, end, "account", user=username)
    c["jobs"] = jobs_q.list_jobs(db, cluster.id, start, end, user=username, limit=50)
    return templates.TemplateResponse(request, "entity.html", c)


@router.get("/c/{cluster}/accounts/{account}", response_class=HTMLResponse)
def account_page(request: Request, account: str, cluster: Cluster = Depends(get_cluster),
                 user: str = Depends(require_user), db: Session = Depends(get_db),
                 from_: str | None = Query(None, alias="from"), to: str | None = None):
    c = ctx(request, db, user, cluster, from_, to, entity_type="account", entity=account)
    start, end = parse_window(from_, to)
    c["summary"] = usage_q.summary(db, cluster.id, start, end, account=account)
    c["members"] = usage_q.top_entities(db, cluster.id, start, end, "user_name", limit=25, account=account)
    c["jobs"] = jobs_q.list_jobs(db, cluster.id, start, end, account=account, limit=50)
    return templates.TemplateResponse(request, "entity.html", c)


@router.get("/c/{cluster}/partitions/{partition}", response_class=HTMLResponse)
def partition_page(request: Request, partition: str, cluster: Cluster = Depends(get_cluster),
                   user: str = Depends(require_user), db: Session = Depends(get_db),
                   from_: str | None = Query(None, alias="from"), to: str | None = None):
    c = ctx(request, db, user, cluster, from_, to, entity_type="partition", entity=partition)
    start, end = parse_window(from_, to)
    c["summary"] = usage_q.summary(db, cluster.id, start, end, partition=partition)
    c["members"] = usage_q.top_entities(db, cluster.id, start, end, "user_name", limit=25, partition=partition)
    c["jobs"] = jobs_q.list_jobs(db, cluster.id, start, end, partition=partition, limit=50)
    return templates.TemplateResponse(request, "entity.html", c)


@router.get("/c/{cluster}/utilization", response_class=HTMLResponse)
def utilization_page(request: Request, cluster: Cluster = Depends(get_cluster), user: str = Depends(require_user),
                     db: Session = Depends(get_db), from_: str | None = Query(None, alias="from"),
                     to: str | None = None, resource: str = "cpu", partition: str | None = None):
    c = ctx(request, db, user, cluster, from_, to, resource=resource, partition=partition or "")
    c["partitions"] = jobs_q.distinct_values(db, cluster.id, "partition")
    return templates.TemplateResponse(request, "utilization.html", c)


@router.get("/c/{cluster}/efficiency", response_class=HTMLResponse)
def efficiency_page(request: Request, cluster: Cluster = Depends(get_cluster), user: str = Depends(require_user),
                    db: Session = Depends(get_db), from_: str | None = Query(None, alias="from"),
                    to: str | None = None, by: str = "user", min_cpu_hours: float = 100.0,
                    sort: str = "wasted_cpu_hours", partition: str | None = None):
    c = ctx(request, db, user, cluster, from_, to, by=by, min_cpu_hours=min_cpu_hours, sort=sort,
            partition=partition or "")
    c["partitions"] = jobs_q.distinct_values(db, cluster.id, "partition")
    return templates.TemplateResponse(request, "efficiency.html", c)


@router.get("/c/{cluster}/fairshare", response_class=HTMLResponse)
def fairshare_page(request: Request, cluster: Cluster = Depends(get_cluster), user: str = Depends(require_user),
                   db: Session = Depends(get_db), from_: str | None = Query(None, alias="from"),
                   to: str | None = None, account: str | None = None, fs_user: str | None = None):
    c = ctx(request, db, user, cluster, from_, to, default_days=90, account=account or "", fs_user=fs_user or "")
    c["latest"] = snap_q.fairshare_latest(db, cluster.id)
    return templates.TemplateResponse(request, "fairshare.html", c)


@router.get("/c/{cluster}/jobs", response_class=HTMLResponse)
def jobs_page(request: Request, cluster: Cluster = Depends(get_cluster), user: str = Depends(require_user),
              db: Session = Depends(get_db), from_: str | None = Query(None, alias="from"), to: str | None = None,
              q_user: str | None = Query(None, alias="user"), account: str | None = None,
              partition: str | None = None, state: str | None = None, job_id: str | None = None,
              gpus_only: bool = False, sort: str = "end_time", desc: bool = True,
              page: int = Query(1, ge=1), per_page: int = Query(100, ge=10, le=500)):
    start, end = parse_window(from_, to, 7)
    result = jobs_q.list_jobs(db, cluster.id, start, end, user=q_user, account=account, partition=partition,
                              state=state, job_id=job_id, gpus_only=gpus_only, sort=sort, desc=desc,
                              limit=per_page, offset=(page - 1) * per_page)
    c = ctx(request, db, user, cluster, from_, to, default_days=7, result=result, page=page, per_page=per_page,
            filters={"user": q_user or "", "account": account or "", "partition": partition or "",
                     "state": state or "", "job_id": job_id or "", "gpus_only": gpus_only},
            sort=sort, desc=desc, pages=max(1, -(-result["total"] // per_page)))
    c["partitions"] = jobs_q.distinct_values(db, cluster.id, "partition")
    c["states"] = jobs_q.distinct_values(db, cluster.id, "state")
    return templates.TemplateResponse(request, "jobs.html", c)


@router.get("/c/{cluster}/nodes", response_class=HTMLResponse)
def nodes_page(request: Request, cluster: Cluster = Depends(get_cluster), user: str = Depends(require_user),
               db: Session = Depends(get_db)):
    c = ctx(request, db, user, cluster, show_window=False)
    c["nodes"] = snap_q.nodes_latest(db, cluster.id)
    c["partition_info"] = snap_q.partitions_latest(db, cluster.id)
    return templates.TemplateResponse(request, "nodes.html", c)


@router.get("/admin/ingest", response_class=HTMLResponse)
def admin_ingest(request: Request, user: str = Depends(require_user), db: Session = Depends(get_db)):
    c = ctx(request, db, user, show_window=False)
    c["status"] = snap_q.ingest_status(db)
    c["recent"] = snap_q.recent_batches(db)
    return templates.TemplateResponse(request, "admin_ingest.html", c)
