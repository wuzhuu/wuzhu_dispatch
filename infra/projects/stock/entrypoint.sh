#!/bin/sh
set -eu
case "${1:-scheduler}" in
  scheduler) exec python -m runtime.scheduler ;;
  health) exec python -m runtime.health ;;
  maintenance) exec python /opt/stock/maintenance/maintenance.py --listen 0.0.0.0:8081 ;;
  *) exec "$@" ;;
esac
