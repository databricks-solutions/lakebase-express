#!/usr/bin/env bash
#
# deploy.sh — build the SPA, then deploy and start Lakebase Express as a
# Databricks App via the bundle in databricks.yml.
#
#   ./deploy.sh                                    # build SPA, deploy bundle, run app
#   ./deploy.sh --skip-build                       # reuse the existing frontend/dist
#   BUNDLE_TARGET=my-workspace ./deploy.sh         # pick a target from target.yml
#   DATABRICKS_PROFILE=my-profile ./deploy.sh      # pin a CLI auth profile
#   APP_NAME=my-app ./deploy.sh                    # override the app name
#
# Targets live in target.yml (copy target.yml.sample); with no BUNDLE_TARGET the
# one marked `default: true` is used.
#
# Prereqs: databricks CLI >= 0.239 (authenticated), node/npm. The app's service
# principal still needs READ on the secret scope + network access to the source
# DB — see README.

set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

# Empty means "use the bundle's own default target".
TARGET="${BUNDLE_TARGET:-}"
SKIP_BUILD=0
[[ "${1:-}" == "--skip-build" ]] && SKIP_BUILD=1

# Targets carry no workspace host, so auth comes from a CLI profile — default to
# the one named after the target.
DATABRICKS_PROFILE="${DATABRICKS_PROFILE:-$TARGET}"

BUNDLE_ARGS=()
[[ -n "$TARGET" ]] && BUNDLE_ARGS+=(-t "$TARGET")
[[ -n "${DATABRICKS_PROFILE:-}" ]] && BUNDLE_ARGS+=(-p "$DATABRICKS_PROFILE")
[[ -n "${APP_NAME:-}" ]] && BUNDLE_ARGS+=(--var "app_name=${APP_NAME}")

# The +"..." guard is required: expanding an empty array under `set -u` is an
# "unbound variable" error on macOS's bash 3.2.
db() { local cmd="$1"; shift; databricks bundle "$cmd" ${BUNDLE_ARGS[@]+"${BUNDLE_ARGS[@]}"} "$@"; }

fail() { echo "✗ $*" >&2; exit 1; }

# --- Prerequisites -----------------------------------------------------------
command -v databricks >/dev/null 2>&1 || fail "databricks CLI not found. Install it and run 'databricks auth login'."

echo "==> Validating bundle (target: ${TARGET:-<bundle default>})"
db validate >/dev/null || fail "Bundle validation failed — check databricks.yml and CLI auth (databricks auth login --host <workspace> --profile <profile>)."

# --- 1. Build the SPA --------------------------------------------------------
if [[ "$SKIP_BUILD" -eq 0 ]]; then
  command -v npm >/dev/null 2>&1 || fail "npm not found (needed to build the frontend). Use --skip-build to reuse frontend/dist."
  echo "==> Building frontend"
  (cd frontend && npm install --silent && npm run build) || fail "Frontend build failed."
fi
[[ -f frontend/dist/index.html ]] || fail "frontend/dist/index.html missing — build the SPA first (don't pass --skip-build)."

# --- 2. Deploy the bundle ----------------------------------------------------
echo "==> Deploying bundle (syncs source, creates/updates the app)"
db deploy || fail "Bundle deploy failed."

# --- 3. Run the app ----------------------------------------------------------
echo "==> Starting the app and deploying its source (first run provisions compute — can take a few minutes)"
db run lakebase_express || fail "Bundle run failed."

# --- Report ------------------------------------------------------------------
# bundle summary has no app URL — ask the Apps API, resolving auth the way the
# bundle did: explicit profile if given, else the profile matching the target host.
WS_HOST="$(db summary -o json 2>/dev/null | python3 -c 'import sys,json; print(json.load(sys.stdin)["workspace"]["host"])' 2>/dev/null)"
if [[ -n "${DATABRICKS_PROFILE:-}" ]]; then
  APP_URL="$(databricks apps get "${APP_NAME:-lakebase-express}" -p "$DATABRICKS_PROFILE" -o json 2>/dev/null | python3 -c 'import sys,json; print(json.load(sys.stdin).get("url",""))' 2>/dev/null)"
else
  APP_URL="$(DATABRICKS_HOST="$WS_HOST" databricks apps get "${APP_NAME:-lakebase-express}" -o json 2>/dev/null | python3 -c 'import sys,json; print(json.load(sys.stdin).get("url",""))' 2>/dev/null)"
fi
echo ""
echo "✓ Deployed '${APP_NAME:-lakebase-express}' (target: ${TARGET:-<bundle default>})."
[[ -n "$APP_URL" ]] && echo "  URL:  $APP_URL"
echo "  Logs: databricks apps logs ${APP_NAME:-lakebase-express}${DATABRICKS_PROFILE:+ --profile $DATABRICKS_PROFILE}"
echo ""
echo "Reminder: the app's service principal needs READ on the '${LBX_SECRET_SCOPE:-lakebase-express}'"
echo "secret scope and network access to the source database (see README)."
