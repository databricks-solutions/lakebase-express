#!/usr/bin/env bash
#
# run_local.sh — start the Lakebase Express backend and frontend together.
#
#   ./run_local.sh                 # install deps, then run
#   ./run_local.sh --no-install    # skip dependency installation
#
# Backend:  uvicorn (FastAPI) on http://127.0.0.1:8000  (serves /api/*)
# Frontend: Vite dev server on http://127.0.0.1:5173     (proxies /api -> :8000)
#
# Ctrl-C stops both. Backend logs are written to /tmp/lbx-backend.log.

set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
BACKEND_LOG="/tmp/lbx-backend.log"
INSTALL=1
[[ "${1:-}" == "--no-install" ]] && INSTALL=0

# --- Activate the Python virtualenv if one exists ----------------------------
if [[ -f "$ROOT_DIR/.venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.venv/bin/activate"
fi

# --- Install dependencies (skip with --no-install) ---------------------------
if [[ "$INSTALL" -eq 1 ]]; then
  echo "==> Installing backend dependencies"
  pip install -q -r requirements.txt
  echo "==> Installing frontend dependencies"
  (cd frontend && npm install --silent)
fi

# --- Tear both processes down together ---------------------------------------
BACKEND_PID=""
FRONTEND_PID=""
cleanup() {
  echo ""
  echo "==> Shutting down..."
  [[ -n "$FRONTEND_PID" ]] && kill "$FRONTEND_PID" 2>/dev/null
  [[ -n "$BACKEND_PID"  ]] && kill "$BACKEND_PID"  2>/dev/null
  wait 2>/dev/null
}
trap cleanup EXIT INT TERM

# --- Backend -----------------------------------------------------------------
# Bind to 127.0.0.1 (not 0.0.0.0) and watch ONLY backend/ — watching the repo
# root would also watch node_modules/.venv/dist and can kill the reload watcher.
echo "==> Starting backend on http://127.0.0.1:${BACKEND_PORT} (log: ${BACKEND_LOG})"
uvicorn backend.main:app \
  --host 127.0.0.1 \
  --port "$BACKEND_PORT" \
  --reload \
  --reload-dir backend \
  > "$BACKEND_LOG" 2>&1 &
BACKEND_PID=$!

# Wait for the backend to actually listen before starting the frontend, so the
# first /api polls don't hit a connection-refused.
echo "==> Waiting for backend to come up..."
for _ in $(seq 1 30); do
  if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
    echo "!! Backend exited during startup. Last log lines:"
    tail -n 30 "$BACKEND_LOG"
    exit 1
  fi
  if curl -sf "http://127.0.0.1:${BACKEND_PORT}/api/projects" >/dev/null 2>&1 \
     || curl -s -o /dev/null "http://127.0.0.1:${BACKEND_PORT}/" 2>/dev/null; then
    break
  fi
  sleep 0.5
done
echo "==> Backend is up."

# --- Frontend ----------------------------------------------------------------
echo "==> Starting frontend on http://127.0.0.1:${FRONTEND_PORT}"
(cd frontend && npm run dev -- --port "$FRONTEND_PORT") &
FRONTEND_PID=$!

echo ""
echo "Backend:  http://127.0.0.1:${BACKEND_PORT}  (logs: ${BACKEND_LOG})"
echo "Frontend: http://127.0.0.1:${FRONTEND_PORT}"
echo "Press Ctrl-C to stop both."

# Portable wait loop (macOS Bash 3.2 has no `wait -n`): exit as soon as either
# process dies, which triggers cleanup() to stop the other.
while kill -0 "$BACKEND_PID" 2>/dev/null && kill -0 "$FRONTEND_PID" 2>/dev/null; do
  sleep 1
done
