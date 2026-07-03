#!/usr/bin/env bash
# Start the dispatcher (FastAPI server)
set -euo pipefail

cd "$(dirname "$0")/../dispatcher"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
