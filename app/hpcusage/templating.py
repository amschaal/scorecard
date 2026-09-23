"""Jinja2 environment + filters shared by page routers."""

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates
from jinja2 import pass_context

from . import __version__

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(PACKAGE_DIR, "templates")
STATIC_DIR = os.path.join(PACKAGE_DIR, "static")
templates = Jinja2Templates(directory=TEMPLATE_DIR)


def fmt_num(v, digits: int = 0) -> str:
    if v is None:
        return "–"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    if digits == 0:
        return f"{v:,.0f}"
    return f"{v:,.{digits}f}"


def fmt_pct(v, digits: int = 0) -> str:
    return "–" if v is None else f"{100 * float(v):.{digits}f}%"


def fmt_duration(seconds) -> str:
    if seconds is None:
        return "–"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60:02d}m"
    return f"{s // 86400}d {(s % 86400) // 3600:02d}h"


def fmt_mb(mb) -> str:
    if mb is None:
        return "–"
    mb = float(mb)
    if mb >= 1024 * 1024:
        return f"{mb / 1024 / 1024:.1f} TB"
    if mb >= 1024:
        return f"{mb / 1024:.1f} GB"
    return f"{mb:.0f} MB"


def _zone(name: str | None):
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError):
        return None


@pass_context
def fmt_ts(ctx, v, tz: str | None = None) -> str:
    """Render an aware timestamp in local time: the `tz` argument if given (per-row, e.g. an ingest
    row's cluster), else the page's `tz` (its cluster's zone). Timestamps are stored in UTC."""
    if not v:
        return "–"
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v)
        except ValueError:
            return v
    zone = _zone(tz) or _zone(ctx.get("tz"))
    if zone is not None:
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        v = v.astimezone(zone)
    return v.strftime("%Y-%m-%d %H:%M")


templates.env.filters.update(num=fmt_num, pct=fmt_pct, duration=fmt_duration, mb=fmt_mb, ts=fmt_ts)
templates.env.globals.update(app_version=__version__)
