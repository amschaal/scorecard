"""The hand-written migration must match the SQLAlchemy models exactly."""

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext


def test_models_match_migrations(engine):
    from hpcusage.models import Base

    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True, "compare_server_default": False})
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == [], f"models and migrations differ: {diff}"
