#!/bin/sh
# Real model verification with no OneBot network or QQ token mounted.
set -eu
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
case_name=${1:?Usage: smoke.sh boundary|search|panel|conversation|memory-quality|addressing|continuity|voice|model-comparison|social|social-read|social-self}
case "$case_name" in
  boundary) report=output-boundary-smoke.json ;;
  search) report=search-display-smoke.json ;;
  panel) report=panel-smoke.json ;;
  conversation) report=bot-memory-smoke.json ;;
  memory-quality) report=memory-quality-smoke.json ;;
  addressing) report=addressing-smoke.json ;;
  continuity) report=continuity-smoke.json ;;
  voice) report=voice-smoke.json ;;
  model-comparison) report=model-comparison.json ;;
  social) report=social-smoke.json ;;
  social-read) report=social-read-smoke.json ;;
  social-self) report=social-self-smoke.json ;;
  *) echo "Unknown smoke case" >&2; exit 2 ;;
esac
container="qqbot-smoke-$case_name-$$"
trap 'docker rm -f "$container" >/dev/null 2>&1 || true' EXIT INT TERM
report_dir=${SMOKE_EVIDENCE_DIR:-$project_root/evidence/refactor}
mkdir -p "$report_dir"
if [ "$case_name" = social-read ] || [ "$case_name" = social-self ]; then
  # Explicit space API probes, separate from model tests and production journals.
  probe_script=qzone_read_smoke.py
  if [ "$case_name" = social-self ]; then probe_script=qzone_self_smoke.py; fi
  docker run --rm --network qq-deepseek-bot_default \
    -e ONEBOT_TOKEN_FILE=/run/secrets/onebot_token -e SMOKE_REPORT_DIR=/reports \
    -v "$project_root:/workspace:ro" -v "$report_dir:/reports" \
    -v "${BOT_STATE_DIR:-/srv/qqbot/bot}/secrets/onebot_token:/run/secrets/onebot_token:ro" \
    --read-only --tmpfs /tmp:size=32m,uid=1000,gid=1000 --cap-drop ALL \
    "${BOT_IMAGE:-qqbot:local}" python "/workspace/tests/integration/$probe_script"
  test -s "$report_dir/$report"
  exit 0
fi
docker create --name "$container" --network qq-memory-addon_egress \
  --env-file "${BOT_ENV_FILE:-/srv/qqbot/bot/.env}" \
  -e DEEPSEEK_API_KEY_FILE=/run/secrets/deepseek_api_key \
  -e MEMORY_TOKEN_FILE=/run/secrets/memory_token \
  -e YUNZAI_TOKEN_FILE=/run/secrets/yunzai_token \
  -e PLUGIN_TOKEN_FILE=/run/secrets/plugin_token \
  -e SMOKE_REPORT_DIR=/reports \
  -e SMOKE_PANEL_UID="${SMOKE_PANEL_UID:-}" -e SMOKE_PANEL_CHARACTER="${SMOKE_PANEL_CHARACTER:-雷神}" \
  -e SMOKE_COMPARISON_MODEL="${SMOKE_COMPARISON_MODEL:-}" \
  -v "$project_root:/workspace:ro" \
  -v "$report_dir:/reports" \
  -v "${BOT_STATE_DIR:-/srv/qqbot/bot}/secrets/deepseek_api_key:/run/secrets/deepseek_api_key:ro" \
  -v "${BOT_STATE_DIR:-/srv/qqbot/bot}/secrets/plugin_token:/run/secrets/plugin_token:ro" \
  -v "${MEMORY_STATE_DIR:-/srv/qqbot/memory}/secrets/memory_token:/run/secrets/memory_token:ro" \
  -v "${PANEL_STATE_DIR:-/srv/qqbot/panel}/secrets/bridge_token:/run/secrets/yunzai_token:ro" \
  --read-only --tmpfs /tmp:size=64m,uid=1000,gid=1000 --cap-drop ALL \
  "${BOT_IMAGE:-qqbot:local}" python /workspace/scripts/real_smoke.py "$case_name" >/dev/null
for network in qq_memory_link qq_yunzai_link qq-deepseek-bot_plugin_link; do
  docker network connect "$network" "$container"
done
docker start -a "$container"
exit_code=$(docker inspect -f '{{.State.ExitCode}}' "$container")
if [ "$exit_code" != 0 ]; then exit "$exit_code"; fi
if [ -n "$report" ]; then
  test -s "$report_dir/$report"
fi
