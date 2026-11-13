#!/bin/sh
# Disposable database only; no volumes, no application/experiment databases.
set -eu
container=$(docker run --detach --rm --publish 127.0.0.1::5432 \
  --env POSTGRES_USER=t02 --env POSTGRES_PASSWORD=t02-test-only \
  --env POSTGRES_DB=t02_tests pgvector/pgvector:0.8.1-pg17-trixie)
trap 'docker stop "$container" >/dev/null' EXIT HUP INT TERM
attempt=0
until docker exec "$container" pg_isready -U t02 -d t02_tests >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then docker logs "$container"; exit 1; fi
  sleep 1
done
port=$(docker port "$container" 5432/tcp | sed 's/.*://')
NOTE_AGENT_TEST_DATABASE_URL="postgresql://t02:t02-test-only@127.0.0.1:$port/t02_tests" \
  "${UV:-uv}" run --directory backend --locked pytest tests/postgres "$@"
