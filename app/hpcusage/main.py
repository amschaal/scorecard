import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from . import __version__
from .db import get_engine, session_factory
from .deps import NotAuthenticated, NotAuthorized
from .ingest.service import get_or_create_cluster
from .routers import api, auth, ingest, pages
from .settings import get_settings
from .templating import STATIC_DIR

log = logging.getLogger("hpcusage")


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        _seed_clusters(settings)
        yield

    app = FastAPI(title="hpcusage", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json",
                  lifespan=lifespan)
    app.add_middleware(
        SessionMiddleware, secret_key=settings.session_secret, session_cookie="hpcusage_session",
        max_age=settings.session_max_age_s, same_site="lax", https_only=settings.is_https,
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    app.include_router(auth.router)
    app.include_router(ingest.router)
    app.include_router(api.router)
    app.include_router(pages.router)

    @app.exception_handler(NotAuthenticated)
    async def _not_authenticated(request: Request, exc: NotAuthenticated):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "not authenticated"}, status_code=401)
        return RedirectResponse(f"/auth/login?next={exc.next_url}", status_code=302)

    @app.exception_handler(NotAuthorized)
    async def _not_authorized(request: Request, exc: NotAuthorized):
        return JSONResponse({"detail": f"{exc.username} is not authorized"}, status_code=403)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok", "version": __version__}

    return app


def _seed_clusters(settings) -> None:
    """Pre-create clusters named in CLUSTERS/COLLECTOR_TOKENS so pages work before the first ingest."""
    names = set(settings.cluster_config) | set(settings.collector_token_map)
    if not names:
        return
    db = session_factory()()
    try:
        for name in sorted(names):
            get_or_create_cluster(db, name, settings)
        db.commit()
    except Exception as e:  # noqa: BLE001 — a DB that is not up yet must not crash startup
        log.warning("could not seed clusters at startup: %s", e)
        db.rollback()
    finally:
        db.close()


app = create_app()
