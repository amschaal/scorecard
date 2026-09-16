"""Backend tests run against a throwaway PostgreSQL started with testcontainers (needs Docker).

Set HPCUSAGE_TEST_DATABASE_URL to use an existing PostgreSQL server instead (docker compose does
this for the app container). The database named in the URL is created if missing, migrated, and
TRUNCATED between tests, so its name must contain "test"."""

import gzip
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(APP_DIR)
sys.path.insert(0, APP_DIR)
sys.path.insert(0, os.path.join(ROOT, "collector"))

FIXTURES = os.path.join(ROOT, "collector", "tests", "fixtures")
TOKENS = {"hive": "test-token-hive", "farm": "test-token-farm"}


def _ensure_test_database(url: str) -> None:
    """Create the test database on the server if it does not exist yet; refuse non-test names."""
    from sqlalchemy.engine import make_url

    u = make_url(url)
    if "test" not in (u.database or ""):
        raise RuntimeError(f"HPCUSAGE_TEST_DATABASE_URL must name a *test* database (got {u.database!r}); "
                           "the suite truncates every table")
    admin = create_engine(u.set(drivername="postgresql+psycopg", database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": u.database}).scalar()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{u.database}"'))
    admin.dispose()


@pytest.fixture(scope="session")
def database_url():
    url = os.environ.get("HPCUSAGE_TEST_DATABASE_URL")
    if url:
        _ensure_test_database(url)
        yield url
        return
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16", driver="psycopg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
def settings_env(database_url):
    os.environ.update({
        "DATABASE_URL": database_url,
        "AUTH_MODE": "dev",
        "DEV_USER": "tester",
        "SESSION_SECRET": "test-secret",
        "COLLECTOR_TOKENS": json.dumps(TOKENS),
        "CLUSTERS": json.dumps({"hive": {"display_name": "Hive", "timezone": "America/Los_Angeles"}}),
        "JOB_RETENTION_DAYS": "400",
        "APP_BASE_URL": "http://testserver",
    })
    from hpcusage.settings import get_settings

    get_settings.cache_clear()
    return get_settings()


@pytest.fixture(scope="session")
def migrated(settings_env):
    from hpcusage.migrate import main as migrate

    migrate()
    return True


@pytest.fixture(scope="session")
def engine(settings_env, migrated):
    return create_engine(settings_env.database_url)


@pytest.fixture()
def clean_db(engine):
    with engine.begin() as conn:
        conn.execute(text(
            "TRUNCATE jobs, daily_usage, daily_partition_util, node_snapshots, partition_snapshots, "
            "fairshare_snapshots, ingest_batches, clusters RESTART IDENTITY CASCADE"
        ))
    yield engine


@pytest.fixture()
def client(clean_db, settings_env):
    from fastapi.testclient import TestClient

    from hpcusage.main import create_app

    with TestClient(create_app()) as c:
        yield c


def gz(payload: dict) -> bytes:
    return gzip.compress(json.dumps(payload).encode())


def post_envelope(client, env: dict, token: str | None = None):
    token = token or TOKENS[env["cluster"]]
    return client.post(f"/api/v1/ingest/{env['kind']}", content=gz(env),
                       headers={"Authorization": f"Bearer {token}", "Content-Encoding": "gzip",
                                "Content-Type": "application/json"})


def envelope_from_fixture(kind: str, fixture: str, cluster: str = "hive", day: date = date(2026, 9, 14),
                          taken_at: str = "2026-09-15T02:15:00-07:00") -> dict:
    """Run the real collector's parsers on a fixture file and wrap the result in an envelope."""
    import slurm_collector as sc

    os.environ["TZ"] = "America/Los_Angeles"
    import time
    time.tzset()
    with open(os.path.join(FIXTURES, fixture)) as fh:
        lines = fh.readlines()
    if kind == "jobs":
        start, end = sc.local_midnight(day), sc.local_midnight(day + timedelta(days=1))
        rows = sc.parse_sacct_lines(lines, start, end)
        return sc.make_envelope(cluster, "jobs", rows, "America/Los_Angeles", start, end,
                                datetime.fromisoformat(taken_at), "slurm 26.05.4")
    if kind == "nodes":
        env = sc.make_envelope(cluster, "nodes", sc.parse_node_lines(lines), "America/Los_Angeles",
                               taken_at=datetime.fromisoformat(taken_at))
        with open(os.path.join(FIXTURES, "scontrol_partition.txt")) as fh:
            env["partitions"] = sc.parse_partition_lines(fh.readlines())
        return env
    if kind == "fairshare":
        return sc.make_envelope(cluster, "fairshare", sc.parse_sshare_lines(lines), "America/Los_Angeles",
                                taken_at=datetime.fromisoformat(taken_at))
    raise ValueError(kind)


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)
