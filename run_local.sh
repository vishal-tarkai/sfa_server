#!/usr/bin/env bash
# Start SFA API for local Swagger: http://127.0.0.1:8080/docs
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/uvicorn ]]; then
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
fi
echo "Swagger UI: http://127.0.0.1:8080/docs"
echo "Health:     http://127.0.0.1:8080/v1/health"
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload
