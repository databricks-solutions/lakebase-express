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
#   LBX_SECRET_ACL=READ ./deploy.sh                # grant READ instead of WRITE
#   LBX_SKIP_ACL=1 ./deploy.sh                     # don't touch the scope's ACL
#
# Targets live in target.yml (copy target.yml.sample); with no BUNDLE_TARGET the
# one marked `default: true` is used.
#
# The app runs as its own service principal, so step 3 grants that SP access to
# the secret scope holding the DB passwords (WRITE by default — the Fernet key is
# generated on first use; see README 'Secret scope access'). Setting the ACL needs
# MANAGE on the scope; without it the script prints the command and carries on.
#
# Step 4 then checks the scope actually holds the Lakebase role password, which the
# app needs under the default projects_backend=postgres. Neither step ever fails the
# deploy: they warn with the command to run and continue.
#
# Prereqs: databricks CLI >= 0.239 (authenticated), node/npm. The app still needs
# network access to the source DB (firewall rule for its egress IP, or private
# link) — see README.

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

# --- 3. Grant the app's service principal access to the secret scope ---------
# Deliberately between deploy and run: `db deploy` creates the app (so its SP
# exists), and the app reads the scope at process start, which `db run` triggers.
# Granting here means the first start already has access — no restart needed.
ACL_PERM="$(printf '%s' "${LBX_SECRET_ACL:-WRITE}" | tr '[:lower:]' '[:upper:]')"
# Read app_name/secret_scope from the bundle, so a target.yml override lands on the
# scope the app actually reads. `value` is present only when set; else `default`.
read_var() { python3 -c '
import sys,json
v = json.load(sys.stdin).get("variables", {}).get(sys.argv[1], {})
print(v.get("value") or v.get("default") or "")' "$1" 2>/dev/null; }
BUNDLE_JSON="$(db validate -o json 2>/dev/null)"
APP="${APP_NAME:-$(printf '%s' "$BUNDLE_JSON" | read_var app_name)}"
APP="${APP:-lakebase-express}"
SCOPE="${LBX_SECRET_SCOPE:-$(printf '%s' "$BUNDLE_JSON" | read_var secret_scope)}"
SCOPE="${SCOPE:-lakebase-express}"
PG_KEY="$(printf '%s' "$BUNDLE_JSON" | read_var projects_pg_secret_key)"
PG_KEY="${PG_KEY:-lakebase-password}"
PROJ_BACKEND="$(printf '%s' "$BUNDLE_JSON" | read_var projects_backend)"

# bundle summary has no app URL/SP — ask the Apps API, resolving auth the way the
# bundle did: explicit profile if given, else the profile matching the target host.
WS_HOST="$(db summary -o json 2>/dev/null | python3 -c 'import sys,json; print(json.load(sys.stdin)["workspace"]["host"])' 2>/dev/null)"
dbx() {
  if [[ -n "${DATABRICKS_PROFILE:-}" ]]; then databricks "$@" -p "$DATABRICKS_PROFILE"
  else DATABRICKS_HOST="$WS_HOST" databricks "$@"; fi
}

# One fetch, two fields: the URL to open and the SP that needs the ACL.
APP_JSON="$(dbx apps get "$APP" -o json 2>/dev/null \
  | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("url","")); print(d.get("service_principal_client_id",""))' 2>/dev/null)"
APP_URL="$(printf '%s\n' "$APP_JSON" | sed -n 1p)"
APP_SP="$(printf '%s\n' "$APP_JSON" | sed -n 2p)"

acl_hint() {
  echo "  Grant it, then restart the app so it re-reads the scope:" >&2
  echo "    databricks secrets put-acl $SCOPE ${APP_SP:-<sp-client-id>} $ACL_PERM${DATABRICKS_PROFILE:+ -p $DATABRICKS_PROFILE}" >&2
  echo "    databricks apps start $APP${DATABRICKS_PROFILE:+ -p $DATABRICKS_PROFILE}" >&2
}

if [[ -n "${LBX_SKIP_ACL:-}" ]]; then
  echo "==> Skipping the secret-scope ACL (LBX_SKIP_ACL set)"
elif [[ -z "$APP_SP" ]]; then
  echo "⚠ Could not read the app's service principal — skipping the secret-scope ACL." >&2
  acl_hint
else
  echo "==> Granting the app's service principal $ACL_PERM on secret scope '$SCOPE'"
  # MANAGE already implies write+read; don't downgrade an intentionally wider grant.
  CURRENT="$(dbx secrets get-acl "$SCOPE" "$APP_SP" -o json 2>/dev/null \
    | python3 -c 'import sys,json; print(json.load(sys.stdin).get("permission",""))' 2>/dev/null)"
  if [[ "$CURRENT" == "MANAGE" || "$CURRENT" == "$ACL_PERM" ]]; then
    echo "  Already granted ($CURRENT) — nothing to do."
  elif ACL_ERR="$(dbx secrets put-acl "$SCOPE" "$APP_SP" "$ACL_PERM" 2>&1)"; then
    echo "  ✓ $ACL_PERM granted to $APP_SP"
  else
    case "$ACL_ERR" in
      *RESOURCE_ALREADY_EXISTS*) echo "  Already granted — nothing to do." ;;
      *RESOURCE_DOES_NOT_EXIST*|*does\ not\ exist*)
        echo "⚠ Secret scope '$SCOPE' does not exist — the app cannot read its credentials." >&2
        echo "  Create it and store the Lakebase role password (see README 'Quick Start' step 3):" >&2
        echo "    databricks secrets create-scope $SCOPE${DATABRICKS_PROFILE:+ -p $DATABRICKS_PROFILE}" >&2
        echo "    databricks secrets put-secret $SCOPE $PG_KEY${DATABRICKS_PROFILE:+ -p $DATABRICKS_PROFILE}" >&2 ;;
      *PERMISSION_DENIED*|*PERMISSION\ DENIED*)
        echo "⚠ You lack MANAGE on scope '$SCOPE', so the ACL could not be set here." >&2
        acl_hint ;;
      *) echo "⚠ Could not set the secret-scope ACL: $ACL_ERR" >&2; acl_hint ;;
    esac
  fi
fi

# --- 4. Check the scope holds the key the app needs --------------------------
# Warn before starting rather than let the app fail on its first request. Only
# lists key names (never values), and only when the Postgres store is in play —
# the other backends never read this key.
if [[ "$PROJ_BACKEND" == "postgres" ]]; then
  # Print one key per line, prefixed "ok" so an empty scope (no keys at all — the
  # usual "created it, forgot put-secret" case) is distinguishable from a failed
  # list, which is already reported above.
  KEYS="$(dbx secrets list-secrets "$SCOPE" -o json 2>/dev/null \
    | python3 -c 'import sys,json; d=json.load(sys.stdin); print("ok"); print("\n".join(s.get("key","") for s in d))' 2>/dev/null)"
  if [[ "${KEYS%%$'\n'*}" == "ok" ]] && ! printf '%s\n' "${KEYS#ok$'\n'}" | grep -qxF "$PG_KEY"; then
    echo "⚠ Scope '$SCOPE' has no '$PG_KEY' key. With projects_backend=postgres the app" >&2
    echo "  fails every request with 'ResourceDoesNotExist: Failed to get secret $PG_KEY'." >&2
    echo "  Store the Lakebase role password (prompts on stdin), then restart the app:" >&2
    echo "    databricks secrets put-secret $SCOPE $PG_KEY${DATABRICKS_PROFILE:+ -p $DATABRICKS_PROFILE}" >&2
    echo "    databricks apps start $APP${DATABRICKS_PROFILE:+ -p $DATABRICKS_PROFILE}" >&2
  fi
fi

# --- 5. Run the app ----------------------------------------------------------
echo "==> Starting the app and deploying its source (first run provisions compute — can take a few minutes)"
db run lakebase_express || fail "Bundle run failed."

# --- Report ------------------------------------------------------------------
# Re-read the URL: on a first deploy the app had none until it started above.
[[ -z "$APP_URL" ]] && APP_URL="$(dbx apps get "$APP" -o json 2>/dev/null \
  | python3 -c 'import sys,json; print(json.load(sys.stdin).get("url",""))' 2>/dev/null)"
echo ""
echo "✓ Deployed '$APP' (target: ${TARGET:-<bundle default>})."
[[ -n "$APP_URL" ]] && echo "  URL:  $APP_URL"
echo "  Logs: databricks apps logs $APP${DATABRICKS_PROFILE:+ --profile $DATABRICKS_PROFILE}"
echo ""
echo "Reminder: the app also needs network access to the source database — a firewall"
echo "rule for its egress IP, or private link (see README)."
