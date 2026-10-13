#!/bin/sh
# Run each isolated runtime with only its dependency set and fictional graph actors.
set -eu
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
docker run --rm --network none -v "$project_root:/workspace:ro" \
  "${BOT_IMAGE:-qqbot:local}" python /workspace/scripts/check.py bot --python-only
docker run --rm --network none -v "$project_root:/workspace:ro" \
  --workdir /workspace "${PANEL_IMAGE:-qq-yunzai-addon:local}" node --test tests/unit/test_protocol.mjs
docker run --rm --network qq-memory-addon_state -v "$project_root:/workspace:ro" \
  -v "${MEMORY_STATE_DIR:-/srv/qqbot/memory}/models:/models:ro" \
  "${MEMORY_IMAGE:-qqmemory:local}" python /workspace/scripts/check.py memory
