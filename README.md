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
  (a firewall rule for the app's egress IP, or a private link).
- A **Databricks secret scope** holding the Lakebase role password under
  `lakebase-password` — the app errors on every request without it. The app runs as
  its own service principal, which needs an ACL on that scope; `deploy.sh` grants
  it, provided you hold **MANAGE** there.

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
secret scope/key (including Key Vault-backed scopes on Azure).

The app is bound to exactly **one** Databricks workspace, not selectable in the
UI: locally the CLI profile it was started with, and when deployed the workspace
the App is published in. Restart with a different profile to switch.

## Local development

To iterate on the code without redeploying, run the two processes locally:

```bash
# Backend (FastAPI) — from the repo root
pip install -r requirements.txt
DATABRICKS_CONFIG_PROFILE=<your-profile> uvicorn backend.main:app --reload --port 8000

# Frontend (React + Vite) — separate shell; proxies /api -> :8000
cd frontend && npm install && npm run dev
```

Open the printed Vite URL (default http://localhost:5173).

`DATABRICKS_CONFIG_PROFILE` is a profile from `~/.databrickscfg` and picks the
workspace for the whole session. Settings shows which one is connected.

## Run tests

```bash
pip install pytest httpx   # httpx backs FastAPI's TestClient
pytest tests/
```

## Deploy as a Databricks App

Deployment is an Asset Bundle (`databricks.yml`) orchestrated by `deploy.sh` (see
the [Quick Start](#quick-start) for the end-to-end steps). It builds the SPA, runs
`databricks bundle deploy`, grants the app's service principal access to the secret
scope, then `databricks bundle run` to start the app.

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
> secret scope; doing so needs **MANAGE** on that scope. You must still give the app
> network access to the source DB endpoint (firewall rule for its egress IP, or
> private link). Async-mode runtime scopes need scope create/write. Passwords are
> never stored in clear text.

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
- **Lakebase auth in lakebase-express is native Postgres roles only** — a role name
  and password over the Postgres wire protocol. Databricks identity auth
  (OAuth/OIDC for users, service principals, or groups) is **not** supported yet,
  so target access isn't governed by workspace RBAC and doesn't inherit SSO/MFA or
  short-lived credentials. TLS is always required and passwords are never stored in
  clear text, but a long-lived role password is a shared secret you must rotate
  yourself: use a dedicated least-privilege role, and prefer a **private network
  path** (Private Link / private endpoints) over a public endpoint with an IP
  allowlist. OAuth support is on the [Roadmap](#roadmap).
- Job-offload and Async-mode paths require a live workspace and expect the schema
  plan to have created the target tables. Key Vault-backed runtime scopes work
  only when the keys already exist (Databricks can't write through to them).
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

## Roadmap

Not commitments — the gaps we'd close next, in rough priority order.

- **Databricks identity auth for Lakebase (OAuth/OIDC).** Connect as a Databricks
  user, service principal, or group with short-lived tokens instead of a native
  Postgres role password, so target access is governed by workspace identity and
  inherits SSO/MFA and credential rotation.
- **More source connectors** — Oracle, PostgreSQL, MySQL. The scanner is
  connector-agnostic; see [Adding a source connector](#adding-a-source-connector).
- **Multi-user run state.** Run state is in-process memory, so the app is
  single-user today; persisting it would allow concurrent users and multi-worker
  deployments.

## License

Released under the [MIT License](LICENSE) and provided **"as is", without warranty
of any kind**. Not an official Databricks product — see the disclaimer at the top.
