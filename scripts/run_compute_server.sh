#!/usr/bin/env bash
# Start the compute server (worker daemon)
set -euo pipefail

cd "$(dirname "$0")/../compute-server"
exec python -m dispatch_compute_server.main -c "${1:-/etc/wuzhu-dispatch/node.yaml}"
