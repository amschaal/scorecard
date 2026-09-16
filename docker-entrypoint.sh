#!/bin/sh
# Wait for the database, apply migrations (under an advisory lock), then exec the server.
set -e
cd /app/app

i=0
until python -c "import sqlalchemy, hpcusage.settings as s; sqlalchemy.create_engine(s.get_settings().database_url).connect().close()" 2>/dev/null; do
  i=$((i+1))
  if [ "$i" -ge 30 ]; then echo "database not reachable after 30 attempts" >&2; exit 1; fi
  echo "waiting for database ($i)..."; sleep 2
done

if [ "${SKIP_MIGRATIONS:-0}" != "1" ]; then
  python -m hpcusage.migrate
fi

exec "$@"
