#!/bin/sh
set -eu
cd "$(dirname "$0")"
case "${1:-}" in
  1|2|4)
    export LOAD_WORKERS="$1"
    exec docker compose -f compose.yaml -f load/workers.yaml up -d --no-build --wait app
    ;;
  reset)
    exec docker compose up -d --no-build --wait app
    ;;
  *) printf 'Usage: ./workers.sh 1|2|4|reset\n' >&2; exit 2 ;;
esac
