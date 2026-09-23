"""The hand-written migration must match the SQLAlchemy models exactly."""

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text

# Expression indexes are compared as SQL text, which Postgres re-renders on reflection (casts,
# parentheses), so they are excluded from the autogenerate diff and checked by name below.
EXPRESSION_INDEXES = {"ix_jobs_active_range"}


def _include_object(obj, name, type_, reflected, compare_to):
    return not (type_ == "index" and name in EXPRESSION_INDEXES)


def test_models_match_migrations(engine):
    from hpcusage.models import Base

    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True, "compare_server_default": False,
                                                    "include_object": _include_object})
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == [], f"models and migrations differ: {diff}"


def test_expression_indexes_exist(engine):
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'jobs'")).all()
    defs = {r.indexname: r.indexdef for r in rows}
    assert EXPRESSION_INDEXES <= set(defs), f"missing indexes: {EXPRESSION_INDEXES - set(defs)}"
    assert "USING gist (tstzrange(start_time, end_time))" in defs["ix_jobs_active_range"]
    assert "WHERE" in defs["ix_jobs_active_range"]
