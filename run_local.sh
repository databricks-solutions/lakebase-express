#!/usr/bin/env bash
#
# run_local.sh — start the Lakebase Express backend and frontend together.
#
#   ./run_local.sh                 # install deps, then run
#   ./run_local.sh --no-install    # skip dependency installation
#   ./run_local.sh --verbose       # mirror backend logs here, at DEBUG
#
# Backend:  uvicorn (FastAPI) on http://127.0.0.1:8000  (serves /api/*)
# Frontend: Vite dev server on http://127.0.0.1:5173     (proxies /api -> :8000)
#
# The Databricks workspace comes from DATABRICKS_CONFIG_PROFILE (a profile in
# ~/.databrickscfg) and is fixed for the whole session:
#
#   DATABRICKS_CONFIG_PROFILE=<your-profile> ./run_local.sh
#
# Ctrl-C stops both. Backend logs are always written to /tmp/lbx-backend.log;
# --verbose also streams them to this terminal, so you can watch each /api call
# arrive (uvicorn's access log) instead of tailing the file in another shell.

set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
BACKEND_LOG="/tmp/lbx-backend.log"
INSTALL=1
VERBOSE=0
for arg in "$@"; do
  case "$arg" in
    --no-install)  INSTALL=0 ;;
    -v|--verbose)  VERBOSE=1 ;;
    *) echo "!! Unknown option: $arg (want --no-install and/or --verbose)" >&2; exit 2 ;;
  esac
done

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

# --- Tear all processes down together ----------------------------------------
BACKEND_PID=""
FRONTEND_PID=""
TAIL_PID=""
CLEANED=0
cleanup() {
  # INT/TERM run the trap and then exit, firing it again on EXIT — act once.
  [[ "$CLEANED" -eq 1 ]] && return
  CLEANED=1
  echo ""
  echo "==> Shutting down..."
  # The log streamer is a `tail -f | while read` pipeline in its own process
  # group (set -m below): signal the group so both halves go, and quietly —
  # killing the members individually makes bash print a "Terminated" report.
  [[ -n "$TAIL_PID"     ]] && kill -- "-$TAIL_PID" 2>/dev/null
  [[ -n "$FRONTEND_PID" ]] && kill "$FRONTEND_PID" 2>/dev/null
  [[ -n "$BACKEND_PID"  ]] && kill "$BACKEND_PID"  2>/dev/null
  wait 2>/dev/null
}
trap cleanup EXIT INT TERM

# --- Backend -----------------------------------------------------------------
# Bind to 127.0.0.1 (not 0.0.0.0) and watch ONLY backend/ — watching the repo
# root would also watch node_modules/.venv/dist and can kill the reload watcher.
echo "==> Starting backend on http://127.0.0.1:${BACKEND_PORT} (log: ${BACKEND_LOG})"
# LBX_LOG_LEVEL is read by backend/main.py's basicConfig call; passing uvicorn
# --log-level debug would NOT raise the app's own loggers, because that
# basicConfig has already pinned the root handler's level.
[[ "$VERBOSE" -eq 1 ]] && export LBX_LOG_LEVEL=DEBUG
: > "$BACKEND_LOG"   # truncate, so a streamed tail shows only this run
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

# --- Mirror backend logs into this terminal (--verbose) ----------------------
# `read`/`printf` rather than `sed s/^/…/`: sed block-buffers when its stdout is
# a pipe, so lines would appear in 4 KB bursts long after the request they
# describe. `set -m` puts the pipeline in its own process group so cleanup()
# can signal it as a unit; restored right after.
if [[ "$VERBOSE" -eq 1 ]]; then
  set -m
  ( tail -n +1 -f "$BACKEND_LOG" \
      | while IFS= read -r line; do printf '[backend] %s\n' "$line"; done ) &
  TAIL_PID=$!
  set +m
fi

echo ""
echo "Backend:  http://127.0.0.1:${BACKEND_PORT}  (logs: ${BACKEND_LOG})"
echo "Frontend: http://127.0.0.1:${FRONTEND_PORT}"
if [[ -n "${DATABRICKS_CONFIG_PROFILE:-}" ]]; then
  echo "Workspace: CLI profile '${DATABRICKS_CONFIG_PROFILE}'"
else
  echo "Workspace: DATABRICKS_CONFIG_PROFILE is not set — falling back to the"
  echo "           DEFAULT profile / DATABRICKS_HOST. Re-run as:"
  echo "           DATABRICKS_CONFIG_PROFILE=<your-profile> ./run_local.sh"
fi
if [[ "$VERBOSE" -eq 1 ]]; then
  echo "Logging:   DEBUG, streamed here as [backend] …"
else
  echo "Logging:   INFO (re-run with --verbose to stream backend logs here)"
fi
echo "Press Ctrl-C to stop both."

# Portable wait loop (macOS Bash 3.2 has no `wait -n`): exit as soon as either
# process dies, which triggers cleanup() to stop the other.
while kill -0 "$BACKEND_PID" 2>/dev/null && kill -0 "$FRONTEND_PID" 2>/dev/null; do
  sleep 1
done
