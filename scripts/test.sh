#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p evidence
docker compose run --rm --no-deps -T --entrypoint python events test.py 2>&1 | tee evidence/test.log
cp .private/events/test.json evidence/test.json
