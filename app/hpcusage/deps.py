"""FastAPI dependencies shared by routers."""

from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import authz
from .db import get_db
from .models import Cluster
from .settings import Settings, get_settings


class NotAuthenticated(Exception):
    def __init__(self, next_url: str = "/"):
        self.next_url = next_url


class NotAuthorized(Exception):
    def __init__(self, username: str):
        self.username = username


def current_user(request: Request, settings: Settings = Depends(get_settings)) -> str | None:
    if settings.auth_mode == "dev":
        return settings.dev_user
    return request.session.get("user")


def require_user(request: Request, user: str | None = Depends(current_user)) -> str:
    if not user:
        target = request.url.path
        if request.url.query:
            target += "?" + request.url.query
        raise NotAuthenticated(quote(target, safe="/?=&%"))
    if not authz.is_allowed(user):
        raise NotAuthorized(user)
    return user


def get_cluster(cluster: str, db: Session = Depends(get_db)) -> Cluster:
    c = db.scalar(select(Cluster).where(Cluster.name == cluster))
    if c is None:
        raise HTTPException(status_code=404, detail=f"unknown cluster {cluster!r}")
    return c


def list_clusters(db: Session) -> list[Cluster]:
    return list(db.scalars(select(Cluster).order_by(Cluster.name)))
