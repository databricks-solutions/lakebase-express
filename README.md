# Lakebase Express

A Databricks App that guides you through migrating a transactional database to
**Databricks Lakebase** (managed serverless Postgres), through a connector
catalog. It's a **migration engine, not a code generator**: you review and edit
each artifact, and the app applies the schema/code and streams the data into
Lakebase itself, with live progress.

Enabled sources today: **Azure SQL** and **SQL Server**. More (Oracle,
PostgreSQL, MySQL, …) "Coming soon".

> **Disclaimer.** This is **not an official Databricks product or service**. It is
> an independent, community/solution accelerator provided **"as is", without any
> warranty or support commitment**, and carries **no SLA**. Databricks is not
> liable for its use. **You are solely responsible** for reviewing every generated
> artifact, validating the migration against your own requirements, testing on
> non-production data first, securing credentials, and verifying the results
> before relying on them. Migrating databases is inherently risky — always keep
> verified backups of your source and target. See [`LICENSE`](LICENSE) for the
> full terms.

## Prerequisites

- **Databricks CLI** ≥ 0.239, authenticated to your workspace
  (`databricks auth login`) — used to deploy the app.
- **Node.js** and **npm** — to build the React frontend.
- **Python** 3.10+ (tested on 3.13) — for local development and running tests.
- A **Databricks workspace** with a **Lakebase** database instance (the migration
  target).
- A **source database** — Azure SQL or SQL Server — reachable from the app
  (a firewall rule for the app's egress IP, or a private link — see
  [Finding the app's egress IP](#finding-the-apps-egress-ip)).
- A **Databricks secret scope** holding the Lakebase role password under
  `lakebase-password` (see [Secret scope contents](#secret-scope-contents) — the app
  errors on every request without it). The app runs as its own service principal,
  which needs an ACL on that scope; `deploy.sh` grants it, provided you hold
  **MANAGE** there (see [Secret scope access](#secret-scope-access)).

The scan and assessment run without a Databricks workspace; the Foundation Model
features and the Job-offload/Async data paths require one.

## Quick Start

Deploy it as a Databricks App.

```bash
# 1. Authenticate the CLI (pick any profile name)
databricks auth login --host <workspace-url> --profile <your-profile>

# 2. Set your deploy target — which workspace the bundle deploys to.
#    target.yml is gitignored (per-user), so create it from the sample:
cp target.yml.sample target.yml
#    Then edit target.yml:
#      * name the target after your CLI profile (<your-profile>), so deploy.sh
#        matches them automatically;
#      * set projects_pg_host / projects_pg_user to your Lakebase instance and
#        role (required — the app won't start without them).

# 3. Create the secret scope and store the Lakebase role password in it.
#    Required with the default `postgres` project store — the app reads this key
#    the first time it touches the store, and errors on every request without it.
databricks secrets create-scope lakebase-express --profile <your-profile>
databricks secrets put-secret lakebase-express lakebase-password --profile <your-profile>

# 4. Build the SPA, deploy the bundle, grant scope access, and start the app
DATABRICKS_PROFILE=<your-profile> ./deploy.sh
```

`deploy.sh` builds `frontend/dist`, runs `databricks bundle deploy`, grants the
app's service principal `WRITE` on the secret scope, then `databricks bundle run`
to launch the app. When it finishes it prints the app URL — open it and:

1. **New migration** → pick a source connector.
2. **Connections & Target** — enter the source (Azure SQL / SQL Server) and the
   Lakebase target, and test both.
3. Work through **Assessment → Schema & Code → Data Migration → Create Sync** to
   scan, plan, and run the migration.
4. After migrating, use the **Post-migration** modules — **Validation** and
   **Query Parity** — to confirm the target matches the source.

Deployment knobs and the permissions the app's service principal needs are in
[Deploy as a Databricks App](#deploy-as-a-databricks-app). To iterate on the code
without redeploying, see [Local development](#local-development).

## What it does

Work is organized into **migration projects** — a saved unit (source/target
config, object selection, assessment, plan, run history) you create, leave, and
resume. Passwords are never stored; optional workspace-bound secret scope/key
references are. Projects persist to a local dir (dev) or a UC volume
(`LBX_PROJECTS_BACKEND=volume`) or Lakebase (`=postgres`).

Modules are **independent and always enabled** — no forced sequence. Connections
are configured once and reused; other modules show a soft hint if something is
missing.

| Module | What it does |
|--------|--------------|
| **Connection & Assessment** | Deterministic scan (schema, data types, T-SQL objects) + rule-based compatibility report, augmented by an AI migration analysis (complexity, effort, deeper risks) |
| **Sizing & Cost** | Map source capacity → Lakebase CUs + cost |
| **Schema & Code** | Build an editable plan (DDL + AI-translated code) and apply it to Lakebase |
| **Data Migration** | Stream data into Lakebase (batched `COPY`) with live per-table progress |
| **Create Sync** | Sync now (in-app) or offload a re-runnable PySpark snapshot to a Databricks Job (run now, create unstarted, or schedule) |
| **Validation** *(post-migration)* | Re-scan both sides and match every object — existence, structure, exact row counts — then remediate with an autonomous AI repair agent, one-shot AI fixes, or manual SQL |
| **Query Parity** *(post-migration)* | Generate N synthetic read-only queries, run each against both sides, and compare row count, result format, and performance — with a side-by-side result preview on any mismatch |

Target identifiers are lower-cased by default (PostgreSQL convention); a project
can instead **preserve source casing** (double-quoted, case-sensitive). System
objects (`sys`, `INFORMATION_SCHEMA`, `is_ms_shipped`, …) are never migrated.

## How it works

- **Backend** (`backend/`) is pure Python with no web framework in the migration
  logic, so it's reusable from notebooks and unit-testable without Databricks.
  The source scan uses `pymssql` (no Spark, no system ODBC driver) and reads only
  small catalog result sets.
- **Schema & code** — the assessment becomes editable `PlanItem`s applied to
  Lakebase in dependency order, each in its own transaction so a failure is
  isolated and reported.
- **Data** — tables stream via `fetchmany` + bulk `COPY` on a background thread
  the UI polls; large tables can be offloaded to a Databricks Job or a PySpark
  **snapshot** (range-partitioned reads → per-partition `COPY`, tables loaded
  concurrently, idempotent re-runs).
- **Constraints, indexes, FKs, and triggers** are created **after** the data
  load, so the bulk copy pays no per-row maintenance and identity sequences sync
  to `MAX+1` once rows exist.
- **Post-migration** — Validation re-inventories both sides and diffs them
  through the naming rules; Query Parity runs generated read-only queries on both
  sides and compares the results. Both run on background threads with polled
  progress.

## Architecture

```
app.yaml       Databricks Apps runtime config
backend/       FastAPI + all migration logic (framework-free, unit-testable)
frontend/      React + Vite + TypeScript SPA (built to frontend/dist/)
tests/         Pure-Python unit tests
```

In production the SPA is built and served by the same FastAPI process. Database
passwords are typed per session or referenced by a workspace-bound Databricks
secret scope/key (including Key Vault-backed scopes on Azure). Databricks
authenticates via user OAuth when configured, otherwise the App's injected
service-principal OAuth.

## Local development

To iterate on the code without redeploying, run the two processes locally:

```bash
# Backend (FastAPI) — from the repo root
pip install -r requirements.txt
uvicorn backend.main:app --reload --port 8000

# Frontend (React + Vite) — separate shell; proxies /api -> :8000
cd frontend && npm install && npm run dev
```

Open the printed Vite URL (default http://localhost:5173).

## Run tests

```bash
pip install pytest
pytest tests/
```

## Deploy as a Databricks App

Deployment is an Asset Bundle (`databricks.yml`) orchestrated by `deploy.sh` (see
the [Quick Start](#quick-start) for the end-to-end steps). It builds the SPA, runs
`databricks bundle deploy`, grants the app's service principal access to the secret
scope ([Secret scope access](#secret-scope-access)), then `databricks bundle run` to
start the app.

**Deploy targets** live in `target.yml`, a per-user file that is **gitignored** so
no workspace-specific config is committed. Copy `target.yml.sample` to `target.yml`
and set your target name; `databricks.yml` pulls it in via its `include:` list. No
workspace host is committed — auth comes from the CLI profile named after the
target (deploy.sh default), an explicit `DATABRICKS_PROFILE`, or a `workspace.host`
you add in `target.yml`.

Knobs (see the header of `deploy.sh`): `--skip-build` reuses `frontend/dist`;
`BUNDLE_TARGET=<target>` picks a target from `target.yml`;
`DATABRICKS_PROFILE=<profile>` pins a CLI profile; `APP_NAME=<name>` overrides the
app name; `LBX_SECRET_ACL=READ` and `LBX_SKIP_ACL=1` control the secret-scope grant.
To deploy to another workspace, add a target to `target.yml` and run
`BUNDLE_TARGET=<target> ./deploy.sh`.

> **Permissions.** `deploy.sh` grants the app's service principal access to the
> secret scope (see [Secret scope access](#secret-scope-access)); doing so needs
> **MANAGE** on that scope. You must still give the app network access to the source
> DB endpoint (firewall rule for its egress IP, or private link — see
> [Finding the app's egress IP](#finding-the-apps-egress-ip)). Async-mode runtime
> scopes need scope create/write. Passwords are never stored in clear text.

### Finding the app's egress IP

Azure SQL allowlists by **source IP**, and a deployed app egresses from a different
address than your laptop — so a scan that works locally can fail in the app with
error **40615** (`Client with IP address '…' is not allowed to access the server`).

To find the address, set `egress_probe` and redeploy. The app then logs its public
egress IP once at startup:

```yaml
# target.yml
variables:
  egress_probe: "true"
```

```bash
./deploy.sh && databricks apps logs <app-name> --profile <your-profile> | grep -i egress
```

**It is off by default** — it's a debug aid, it calls out to third-party echo
services (ipify, checkip.amazonaws.com, ifconfig.me), and a healthy deployment
doesn't need it. Turn it off again once the firewall rule is in place. Anything
other than `1`/`true`/`yes`/`on` leaves it disabled, so a typo fails closed.

The probe queries three independent services so it can tell a *stable* egress from
a rotating one:

| Log line | Meaning |
|----------|---------|
| `INFO … Egress IP: <one address>` | Stable — allowlist that address |
| `WARNING … multiple addresses seen (…)` | Rotating serverless egress; a single firewall rule is fragile — prefer "Allow Azure services" or a private endpoint |
| `WARNING … could not determine` | No echo service reachable — outbound HTTPS is blocked, or the probe itself has no egress |

The `WARNING` is the probe working as intended, not an error in the migration: it
tells you a one-IP rule *won't hold*. Don't confuse it with a connection failure —
error **40613** (`Database unavailable`) is the source database being paused or
still resuming, and has nothing to do with the firewall.

### Secret scope contents

The scope named by `var.secret_scope` (default `lakebase-express`) holds the keys
below. `deploy.sh` grants the app access to the scope but **cannot create the keys**
— it has no way to know your passwords, so this part stays manual.

| Key | Who reads it | Required? |
|-----|--------------|-----------|
| `lakebase-password` (`var.projects_pg_secret_key`) | `backend/projects/store.py` — the Postgres project store's own connection | **Yes**, with the default `projects_backend: postgres` |
| `lbx-credential-key` (`var.credential_key_secret_key`) | `backend/connectors/credential_store.py` — Fernet key encrypting stored passwords | No — auto-generated on first use if the app has WRITE |
| *your DB passwords*, e.g. `azuresql-password` | Nothing at startup. These are the keys you pick in the UI's secret-scope fields, and in generated Job code | No — convenience only |

A missing `lakebase-password` surfaces on **every request that touches a project**,
not at startup — the store is constructed lazily and the result cached, so the app
starts and passes health checks, then fails as soon as you open it:

```
ResourceDoesNotExist: Failed to get secret lakebase-password for scope lakebase-express
```

Fix it by storing the key, then **restarting the app** — `get_store()` is
`lru_cache`d, so a process that already failed will not pick up a newly added
secret:

```bash
databricks secrets put-secret <scope> lakebase-password -p <your-profile>
databricks apps start <app-name> -p <your-profile>
```

The value is the password for the Lakebase role in `var.projects_pg_user`. Pass it
on **stdin** (the command prompts) rather than `--string-value`, which records it in
your shell history. Alternatively set `LBX_PROJECTS_PG_PASSWORD` in the app's env to
bypass the scope entirely — but that stores the password in plain text in the app
config, so the secret scope is preferred.

Distinguish the two failure modes by the error type: `ResourceDoesNotExist` means
the scope or key is absent (this section), while `PermissionDenied` means they exist
but the app's identity can't read them ([Secret scope access](#secret-scope-access)).

To avoid `lakebase-password` altogether, run with a different project store —
`projects_backend: local` or `volume` in `target.yml` needs no Postgres connection
and never reads this key.

### Secret scope access

A Databricks App runs as its **own service principal**, not as the developer who
deployed it — so your CLI's access to a scope says nothing about the app's. The SP
needs an explicit ACL on the scope named by `var.secret_scope` (default
`lakebase-express`).

**`deploy.sh` does this for you** (step 3, between `bundle deploy` and
`bundle run`): `deploy` creates the app so its SP exists, and the app reads the
scope when its process starts, which `run` triggers — so granting in between means
the first start already has access and no restart is needed. The step is
idempotent, never downgrades an existing `MANAGE`, and on any failure warns with
the exact command to run by hand rather than aborting the deploy.

| Env var | Effect |
|---------|--------|
| `LBX_SECRET_ACL=READ` | Grant `READ` instead of `WRITE` (see below) |
| `LBX_SKIP_ACL=1` | Leave the scope's ACLs alone entirely |
| `LBX_SECRET_SCOPE=<name>` | Override the scope to grant on |

The scope and app name are read from the bundle (`bundle validate -o json`), so a
`secret_scope` set in `target.yml` is picked up automatically and the grant always
lands on the scope the app actually reads; the env vars above override it.

To do it manually — a scope managed by someone else, or `LBX_SKIP_ACL=1`:

```bash
# The SP's client_id is the `service_principal_client_id` field on the app.
databricks apps get <app-name> -p <your-profile> -o json | grep service_principal
databricks secrets put-acl <scope> <sp-client-id> WRITE -p <your-profile>
databricks apps start <app-name> -p <your-profile>   # only needed post-start
```

Granting requires **MANAGE** on the scope; the identity that ran `create-scope`
has it. If you lack it, `deploy.sh` prints the command for whoever does. The app
reads the scope lazily and caches the result for the process lifetime, so
**restart the app** after changing ACLs on an already-running app — a cached
failure does not self-heal.

**Why `WRITE` and not `READ`.** Of the two keys the app reads
([Secret scope contents](#secret-scope-contents)), `lakebase-password` only needs
READ — but `lbx-credential-key` is **auto-generated and written back** when the
scope has none, so a fresh scope needs WRITE. With READ only, that write fails —
which is caught and logged (`Could not persist credential`), degrading to the
in-memory cache rather than crashing, so typed passwords simply stop surviving a
restart. `WRITE` includes read, so it satisfies both rows.

To keep the scope **READ-only** for the app, pre-create the Fernet key yourself and
grant READ instead — nothing then needs to write:

```bash
python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())' \
  | databricks secrets put-secret <scope> lbx-credential-key -p <your-profile>
LBX_SECRET_ACL=READ ./deploy.sh    # or put-acl ... READ by hand
```

Rotating that key makes existing stored passwords undecryptable; they are treated
as a cache miss and re-prompted, not an error. Key Vault-backed scopes are
read-only to Databricks, so they always need the key pre-created this way.

Missing ACL surfaces as:

```
PermissionDenied: User <client-id> does not have secret-scopes.secrets/get
permission on scope lakebase-express to perform this action
```

That is an *authentication-adjacent authorization* failure on the app's identity —
check the `client_id` in the error against the app's SP before assuming the scope
or key is wrong. If instead you see `ResourceDoesNotExist`, the scope or key is
genuinely absent (step 3 of the [Quick Start](#quick-start)).

## Adding a source connector

1. **Frontend** — set `enabled: true` and add connection-form hints in
   `frontend/src/connectors.ts`. Optionally drop a logo at
   `frontend/public/logos/<id>.svg`.
2. **Backend** — register the connector's `source_type` in
   `backend/connectors/factory.py`. For a non-T-SQL dialect, add a connector
   exposing `database`, `query(sql) -> list[dict]`, and `test_connection()`; the
   scanner is connector-agnostic.

## Limitations

- Data-type coercion is light: `bit`→`boolean` is handled; other edge types rely
  on psycopg adapters and surface as a per-table error rather than dropping rows.
- Check constraints, defaults, and filtered-index predicates are translated
  mechanically; anything unrecognized passes through verbatim and fails visibly
  at apply time for review.
- Run state is in-process memory — fine for a single-user App; use a table/Redis
  for multi-worker deployments.
- Job-offload and Async-mode paths require a live workspace and expect the schema
  plan to have created the target tables. Key Vault-backed runtime scopes work
  only when the keys already exist (Databricks can't write through to them) — see
  [Secret scope access](#secret-scope-access).
- `money`/`smallmoney` arithmetic differs between the two engines even though the
  type mapping itself is lossless — see
  [`money` and fixed-scale arithmetic](#money-and-fixed-scale-arithmetic).

### `money` and fixed-scale arithmetic

`money`/`smallmoney` map to `numeric(19,4)`/`numeric(10,4)` — lossless: same scale,
full range, no stored value changes. The **arithmetic** differs. T-SQL `AVG()` over a
`money` column [returns `money`][avg], truncating the division to four decimals inside
the aggregate; Postgres `avg(numeric)` rounds at the end instead. Same data, same
query, one digit apart (for example, `254759.0624` vs `254759.0625`).

According to the Microsoft documentation:

> You can experience rounding errors through truncation, when storing monetary values
> as **money** and **smallmoney**. Avoid using this data type if your money or
> currency values are used in calculations. Instead, use the **decimal** data type
> with at least four decimal places.
> — [money and smallmoney (Transact-SQL)][money]

Only division is exposed — `SUM`, `COUNT`, `MIN`, `MAX` are exact on both sides. Worth
reviewing where a divided `money` value is written back to a column and feeds later
calculations, since truncation always biases the same way and accumulates. Converting
`MONEY` → `DECIMAL(19,4)` on the source before migrating avoids it entirely.

Query Parity reports these as mismatches, correctly — the two sides really do return
different values. Expand the row to see the side-by-side preview: matching counts and
sums with a difference confined to the last decimal point to this behaviour rather
than to missing or altered data.

[money]: https://learn.microsoft.com/en-us/sql/t-sql/data-types/money-and-smallmoney-transact-sql
[avg]: https://learn.microsoft.com/en-us/sql/t-sql/functions/avg-transact-sql

## License

Released under the [MIT License](LICENSE) and provided **"as is", without warranty
of any kind**. Not an official Databricks product — see the disclaimer at the top.
