#!/bin/sh
# Acceptance smoke: creates a NEW project volume and leaves it intact after down.
set -eu
cd "$(dirname "$0")/.."
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-backend-smoke-$(date +%s)-$$}"
export POSTGRES_USER=backend_smoke
export POSTGRES_DB=backend_smoke
export POSTGRES_PASSWORD=backend-smoke-only
export NOTE_AGENT_DATABASE_URL=postgresql://backend_smoke:backend-smoke-only@db:5432/backend_smoke
export NOTE_AGENT_PORT="${NOTE_AGENT_PORT:-18081}"
volume="${COMPOSE_PROJECT_NAME}_postgres-data"
originals_volume="${COMPOSE_PROJECT_NAME}_originals-data"
# Fail on daemon errors before interpreting inspect's failure as volume absence.
docker info > /dev/null
if docker volume inspect "$volume" > /dev/null 2>&1 || docker volume inspect "$originals_volume" > /dev/null 2>&1; then
    echo "Refusing to use existing volume: $volume" >&2
    exit 1
fi
compose() { docker compose --env-file /dev/null -p "$COMPOSE_PROJECT_NAME" "$@"; }
cleanup() {
    result=$?
    if [ "$result" -ne 0 ]; then compose logs --no-color; fi
    compose down
    echo "Preserved test volumes: $volume $originals_volume"
    exit "$result"
}
trap cleanup EXIT
compose config --quiet
compose build api
compose up --wait --wait-timeout 120 db
make migrate COMPOSE="docker compose --env-file /dev/null -p $COMPOSE_PROJECT_NAME"
make migrate COMPOSE="docker compose --env-file /dev/null -p $COMPOSE_PROJECT_NAME"
compose up --wait --wait-timeout 120 api
compose exec -T db sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
SELECT extversion FROM pg_extension WHERE extname = 'vector';
CREATE TABLE backend_smoke (value text NOT NULL);
INSERT INTO backend_smoke VALUES ('persistent');
SQL
curl --fail --silent --show-error "http://127.0.0.1:${NOTE_AGENT_PORT}/api/v1/health" | python3 -c 'import json, sys; assert json.load(sys.stdin) == {"status": "ok"}'
# Check the target API without model endpoints, then compare the full note after restart.
check_note() {
    python3 - "$NOTE_AGENT_PORT" "$@" <<'PYTHON'
import json
import sys
from urllib.request import Request, urlopen
from uuid import UUID

base = f"http://127.0.0.1:{sys.argv[1]}/api/v1/notes"
text = "Smoke title\n  Persistent original text  \n"
if len(sys.argv) == 2:
    body = json.dumps({"source_type": "text", "text": text}).encode()
    request = Request(base, data=body, headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=10) as response:
        assert response.status == 201
        expected = json.load(response)
    UUID(expected["id"])
    assert expected["title"] == "Smoke title"
    assert expected["extracted_text"] == expected["source"]["original_text"] == text
    assert expected["source"]["type"] == "text"
    assert expected["ingestion_status"] == "ready"
    assert expected["processing_status"] == "not_requested"
    assert expected["summary"] is None and expected["tags"] == expected["entities"] == []
else:
    expected = json.loads(sys.argv[2])
with urlopen(base + "/" + expected["id"], timeout=10) as response:
    assert response.status == 200
    assert json.load(response) == expected
print(json.dumps(expected))
PYTHON
}
check_file() {
    python3 - "$NOTE_AGENT_PORT" "$@" <<'PYTHON'
import json
import sys
from urllib.request import Request, urlopen

base = f"http://127.0.0.1:{sys.argv[1]}/api/v1"
original = b"\xef\xbb\xbf# Compose original\r\n\r\n  keep whitespace\n"
if len(sys.argv) == 2:
    boundary = "t11-compose-smoke-boundary"
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"source_type\"\r\n\r\ntext_file\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"compose.md\"\r\n"
        "Content-Type: text/markdown\r\n\r\n"
    ).encode() + original + f"\r\n--{boundary}--\r\n".encode()
    request = Request(base + "/notes/import", data=body,
                      headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urlopen(request, timeout=10) as response:
        assert response.status == 202
        expected = json.load(response)
    assert expected["title"] == "compose.md"
    assert expected["ingestion_status"] == "queued"
else:
    expected = json.loads(sys.argv[2])
with urlopen(base + "/notes/" + expected["id"], timeout=10) as response:
    assert json.load(response) == expected
with urlopen("http://127.0.0.1:" + sys.argv[1] + expected["attachments"][0]["download_url"], timeout=10) as response:
    assert response.read() == original
print(json.dumps(expected))
PYTHON
}
note=$(check_note)
file_note=$(check_file)
compose down
compose up --wait --wait-timeout 120 db
compose up --wait --wait-timeout 120 api
check_note "$note" > /dev/null
check_file "$file_note" > /dev/null
value=$(compose exec -T db sh -c 'psql -At -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT value FROM backend_smoke"')
[ "$value" = persistent ]
compose exec -T db sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "DROP TABLE backend_smoke"'
curl --fail --silent --show-error "http://127.0.0.1:${NOTE_AGENT_PORT}/api/v1/health" | python3 -c 'import json, sys; assert json.load(sys.stdin) == {"status": "ok"}'
compose exec -T api python -c 'from pathlib import Path; assert not list(Path("/app").rglob("*.sqlite3"))'
echo "PASS: explicit migrations, pgvector, API health, note create/read, minimal image, PostgreSQL, note and original byte persistence"
