"""Run Alembic migrations under a Postgres advisory lock so concurrently starting
app instances (App Runner rolling deploys) don't race each other.

    python -m hpcusage.migrate
"""

import logging
import os
import sys

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from .settings import get_settings

LOCK_ID = 727_001  # arbitrary but fixed


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    here = os.path.dirname(os.path.abspath(__file__))  # the hpcusage package; alembic/ ships inside it
    cfg = Config(os.path.join(here, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(here, "alembic"))
    engine = create_engine(settings.database_url)
    with engine.connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(:id)"), {"id": LOCK_ID})
        try:
            command.upgrade(cfg, "head")
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": LOCK_ID})
    return 0


if __name__ == "__main__":
    sys.exit(main())
