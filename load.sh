#!/bin/sh
set -eu
cd "$(dirname "$0")"
case "${1:-}" in
  mixed|heavy) ;;
  *) set -- mixed "$@" ;;
esac
exec python3 load/run.py stage "$@"
