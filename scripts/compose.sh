#!/bin/sh
# One complete manifest per stack; deployment settings stay outside source control.
set -eu
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
stack=${1:?Usage: compose.sh bot|memory|panel <compose arguments>}
shift
case "$stack" in
  bot|memory|panel) ;;
  *) echo "Unknown stack: $stack" >&2; exit 2 ;;
esac
env_path=${DEPLOY_ENV_FILE:-$project_root/.env}
exec docker compose --env-file "$env_path" -f "$project_root/deploy/$stack.compose.yaml" "$@"
