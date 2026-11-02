#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 scripts/prepare.py
mkdir -p .private/events evidence
python3 scripts/fetch_model.py
build_args=()
if test "$(docker image inspect python:3.11-slim --format '{{.Id}}' 2>/dev/null || true)" = 'sha256:de8d79606beff46114d4abd5c392116c62142cbe582f8ede92cf4a64e1a10ff5'; then
  build_args+=(--build-arg PYTHON_BASE_IMAGE=python:3.11-slim)
fi
docker compose build "${build_args[@]}" events
docker compose up -d opencloud events
# Wait only for startup; document processing is driven by NATS push events.
docker compose run --rm --no-deps -T --entrypoint python events - <<'PY'
import time
from client import OpenCloud
for attempt in range(120):
    try:
        OpenCloud().call('GET', '/graph/v1.0/me')
        break
    except Exception:
        time.sleep(2)
else:
    raise RuntimeError('OpenCloud non pronto')
print('OpenCloud: https://localhost:9200 — utente admin, password in .env')
PY
printf '%s\n' 'Esegui: bash scripts/test.sh'
