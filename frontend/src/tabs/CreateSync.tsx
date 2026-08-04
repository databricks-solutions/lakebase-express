import { useEffect, useMemo, useRef, useState } from "react";
import { api, POST_DATA_KINDS, type ApplyResponse, type Artifact, type AsyncSetupResult, type PlanItem, type RunState, type SecretScopeOption, type TableInfo, type WorkspaceStatus } from "../api";
import CodeBlock from "../components/CodeBlock";
import SecretScopeField from "../components/SecretScopeField";
import type { MigrationState } from "../App";
import { mapObject, mapSchema } from "../naming";

interface Props {
  state: MigrationState;
  onGoConnection?: () => void;
  onGoSchema?: () => void;
  onGoData?: () => void;
  onGoValidation?: () => void;
  workspace?: WorkspaceStatus | null;
  onManageWorkspace?: () => void;
}

type Mode = "now" | "async";
type Schedule = "once" | "create";
type SyncScope = "schema" | "full";

export default function CreateSync({ state, onGoConnection, onGoSchema, onGoData, onGoValidation, workspace, onManageWorkspace }: Props) {
  const report = state.report;
  const conn = state.connection;

  const [mode, setMode] = useState<Mode>("now");

  // Sync-now execution state.
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [applyRes, setApplyRes] = useState<ApplyResponse | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [run, setRun] = useState<RunState | null>(null);
  // Post-data phase (constraints, indexes, FKs, triggers) — applied after the load.
  const [postApplyRes, setPostApplyRes] = useState<ApplyResponse | null>(null);
  const [postBusy, setPostBusy] = useState(false);
  const postRanRef = useRef(false);
  // What "Sync now" runs: only the schema & code plan (the default), or the full migration.
  const [syncScope, setSyncScope] = useState<SyncScope>("schema");
  // True while/after a schema-&-code-only apply, so progress skips the data phase.
  const [schemaOnly, setSchemaOnly] = useState(false);
  const pollRef = useRef(true);

  // The plan's two phases: pre-data (schemas/tables/code) is applied before the
  // load; post-data (constraints/indexes/FKs/triggers) only after it, so the
  // bulk COPY doesn't pay index-maintenance or FK-validation costs.
  const prePlan = useMemo(
    () => (state.plan ?? []).filter((it) => !POST_DATA_KINDS.has(it.kind)),
    [state.plan],
  );
  const postPlan = useMemo(
    () => (state.plan ?? []).filter((it) => POST_DATA_KINDS.has(it.kind)),
    [state.plan],
  );

  // Async mode config (PySpark snapshot offloaded to a Databricks job).
  const [secretScope, setSecretScope] = useState("lakebase-express");
  const [runtimeScopeOption, setRuntimeScopeOption] = useState<SecretScopeOption | null>(null);
  const [pwdKey, setPwdKey] = useState("azuresql-password");
  const [lakebasePwdKey, setLakebasePwdKey] = useState("lakebase-password");
  const [workspaceDir, setWorkspaceDir] = useState("/Workspace/Shared/lakebase-express");
  const [schedule, setSchedule] = useState<Schedule>("create");
  // Create/update the secret scope from the entered connection passwords on submit.
  const [syncSecrets, setSyncSecrets] = useState(true);
  const [secretMsg, setSecretMsg] = useState<string | null>(null);
  const [asyncBusy, setAsyncBusy] = useState(false);
  const [asyncRes, setAsyncRes] = useState<AsyncSetupResult | null>(null);
  const [preview, setPreview] = useState<Artifact[] | null>(null);
  const [previewBusy, setPreviewBusy] = useState(false);

  useEffect(() => {
    if (!runId) return;
    pollRef.current = true;
    // Phase 3 — constraints & indexes, once the data phase has finished. A
    // fully failed run (setup-level, nothing loaded) skips it; re-running the
    // sync applies everything again (the post-data DDL is idempotent).
    const applyPostPhase = async (r: RunState) => {
      if (postRanRef.current || !postPlan.length || r.status === "failed") {
        setBusy(false);
        return;
      }
      postRanRef.current = true;
      setPostBusy(true);
      try {
        setPostApplyRes(await api.applyPlan({ lakebase: state.lakebase!, items: postPlan, stop_on_error: false }));
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setPostBusy(false);
        setBusy(false);
      }
    };
    const tick = async () => {
      try {
        const r = await api.dataStatus(runId);
        if (!pollRef.current) return;
        setRun(r);
        if (r.status === "running") setTimeout(tick, 1500);
        else applyPostPhase(r);
      } catch (e) {
        if (pollRef.current) { setError((e as Error).message); setBusy(false); }
      }
    };
    tick();
    return () => { pollRef.current = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);

  if (!report || !conn) {
    return (
      <div className="card">
        <h2>Run the assessment first</h2>
        <p className="muted">Create Sync needs the scanned table list and a configured target.</p>
        {onGoConnection && <div className="actions"><button className="btn btn--primary" onClick={onGoConnection}>Go to Connections &amp; Target</button></div>}
      </div>
    );
  }

  const inSelection = (t: TableInfo) =>
    state.selection.length === 0 || state.selection.includes(`${t.schema_name}.${t.table_name}`);
  const selectedTables = report.tables.filter(inSelection);
  const selectedRows = selectedTables.reduce((n, t) => n + t.row_count, 0);
  const overwrite = state.dataOptions.truncate_first;
  const syncModeLabel = overwrite ? "Full refresh · Overwrite" : "Full refresh · Append";

  const groups = useMemo(() => {
    const by: Record<string, TableInfo[]> = {};
    for (const t of selectedTables) (by[t.schema_name] ??= []).push(t);
    return Object.entries(by).sort(([a], [b]) => a.localeCompare(b));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.selection, report.tables]);

  const noTarget = !state.lakebase?.host;
  const noTables = selectedTables.length === 0;
  const running = run?.status === "running";

  // Apply plan items. Failures surface in the summary below; the
  // Post-Migration Validation module's AI repair agent is where they get fixed.
  async function applyItems(items: PlanItem[]): Promise<ApplyResponse> {
    return api.applyPlan({ lakebase: state.lakebase!, items, stop_on_error: false });
  }

  function resetRunState() {
    setError(null); setApplyRes(null); setPostApplyRes(null); setRun(null); setRunId(null);
    postRanRef.current = false;
  }

  // Apply just the schema & code plan to Lakebase — no data load. Applies the
  // whole plan (constraints included — they're fine on empty tables); useful
  // to (re)create the target objects.
  async function applySchemaOnly() {
    if (!state.lakebase || !state.plan?.length) return;
    // Empty password is fine — the backend resolves it from its session cache.
    resetRunState(); setSchemaOnly(true); setBusy(true);
    try {
      setApplyRes(await applyItems(state.plan));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function syncNow() {
    if (!state.lakebase) return;
    if (!conn) {
      setError("No source connection configured — set it on the Connections & Target step.");
      return;
    }
    // Empty passwords are fine — the backend resolves them from its session cache.
    resetRunState(); setSchemaOnly(false); setBusy(true);
    try {
      // Phase 1 — schema & code, pre-data items only (constraints, indexes,
      // FKs, and triggers are applied after the data, in phase 3).
      if (prePlan.length) {
        setApplyRes(await applyItems(prePlan));
      }
      // Phase 2 — data (polled to completion).
      const dr = await api.startData({
        source_type: conn!.source_type, host: conn!.host, database: conn!.database,
        username: conn!.username, password: conn!.password, port: conn!.port,
        secret_ref: conn!.secret_ref,
        project_id: conn!.project_id,
        lakebase: state.lakebase, target_schema: state.targetSchema,
        identifier_case: state.identifierCase,
        truncate_first: state.dataOptions.truncate_first, batch_size: state.dataOptions.batch_size,
        tables: selectedTables.map((t) => ({
          schema_name: t.schema_name, table_name: t.table_name, target_table: t.table_name,
          total_rows: t.row_count, columns: t.columns,
        })),
      });
      setRunId(dr.run_id);
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  }

  function asyncSpec(): Record<string, unknown> {
    return {
      mode: "snapshot",
      host: conn!.host, database: conn!.database, username: conn!.username,
      password_secret_key: pwdKey, secret_scope: secretScope, port: conn!.port,
      target_schema: state.targetSchema,
      identifier_case: state.identifierCase,
      lakebase_host: state.lakebase?.host ?? "",
      lakebase_port: state.lakebase?.port ?? 5432,
      lakebase_database: state.lakebase?.database ?? "lakebase",
      lakebase_user: state.lakebase?.user ?? "",
      lakebase_password_secret_key: lakebasePwdKey,
      // primary_key feeds the notebook's range-partitioned (parallel) source reads.
      tables: selectedTables.map((t) => ({
        schema_name: t.schema_name, table_name: t.table_name, primary_key: t.primary_key ?? [],
      })),
      // Post-data plan items (constraints/indexes/FKs/triggers) — the job
      // applies them itself after the snapshot, since it may run detached
      // from the app. `kind` lets the job split them into one task per type.
      // User edits to the plan SQL ride along verbatim.
      post_load_sql: postPlan
        .filter((it) => it.sql.trim())
        .map((it) => ({ name: it.name, sql: it.sql, kind: it.kind })),
    };
  }

  async function setupAsync() {
    if (!state.lakebase) return;
    setAsyncBusy(true); setError(null); setAsyncRes(null); setApplyRes(null); setPostApplyRes(null); setSecretMsg(null);
    try {
      if (syncSecrets && runtimeScopeOption?.backend_type === "AZURE_KEYVAULT") {
        setError(
          "Azure Key Vault-backed scopes cannot be updated through the Databricks Secrets API. " +
          "Disable credential sync and use existing keys, or choose/create a Databricks-backed runtime scope.",
        );
        return;
      }
      // Provision the secret scope so the generated snapshot job can read the
      // passwords at runtime. Values entered this session are sent directly;
      // blanks (lost on a reload) are resolved server-side from the persisted
      // credential store via the connection descriptors.
      if (syncSecrets) {
        const secrets: Record<string, string> = {};
        if (conn!.password) secrets[pwdKey] = conn!.password;
        if (state.lakebase.password) secrets[lakebasePwdKey] = state.lakebase.password;
        const s = await api.ensureSecrets({
          scope: secretScope,
          secrets,
          source: conn!,
          lakebase: state.lakebase,
          source_key: pwdKey,
          lakebase_key: lakebasePwdKey,
        });
        const wrote = new Set(s.keys);
        // Only now can we tell if a password was truly unavailable (neither
        // entered nor stored) — surface a precise, actionable error.
        const missing: string[] = [];
        if (!wrote.has(pwdKey)) missing.push("source");
        if (!wrote.has(lakebasePwdKey)) missing.push("Lakebase");
        if (missing.length) {
          setError(`Missing ${missing.join(" and ")} password${missing.length > 1 ? "s" : ""} — ` +
            "not entered this session and none stored yet. Re-enter them in the Connection step " +
            "(test the connection to store it), then retry.");
          setAsyncBusy(false);
          return;
        }
        setSecretMsg(`Secret scope "${s.scope}" ${s.created ? "created" : "updated"} — keys: ${s.keys.join(", ")}.`);
      }
      // Reuse the Sync-mode plan's PRE-data items for schema & code; the
      // post-data items (constraints/indexes/FKs/triggers) are embedded in the
      // snapshot notebook and applied by the job after the copy.
      if (prePlan.length) {
        setApplyRes(await applyItems(prePlan));
      }
      const r = await api.asyncSetup({
        spec: asyncSpec(),
        workspace_dir: workspaceDir,
        quartz_cron: null,
        timezone: "UTC",
        run_now: schedule === "once",
      });
      setAsyncRes(r);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setAsyncBusy(false);
    }
  }

  async function previewNotebooks() {
    setPreviewBusy(true); setError(null); setPreview(null);
    try {
      const r = await api.generateETL(asyncSpec());
      setPreview(r.artifacts);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setPreviewBusy(false);
    }
  }

  return (
    <div className="stack">
      <section className="card">
        <h2>Select sync mode</h2>
        <p className="muted">How do you want to run this migration?</p>
        <div className="modecards">
          <ModeCard
            on={mode === "now"} onSelect={() => setMode("now")} badge="In-app" title="Sync now"
            desc="Run the full migration now in this app — create the schema & code in Lakebase, stream the selected data, then add constraints & indexes, with live progress."
          />
          <ModeCard
            on={mode === "async"} onSelect={() => setMode("async")} badge="Offloaded" title="Async mode"
            desc="Apply the same schema & code plan, then snapshot the selected data into Lakebase with a parallel PySpark job on Databricks (the job adds constraints & indexes after the copy) — run it now, or create it to run on compute you pick. Scales beyond the in-app loader."
          />
        </div>
      </section>

      <section className="card">
        <div className="card__head">
          <h3>Streams</h3>
          <span className="muted">
            {selectedTables.length.toLocaleString()} streams · {selectedRows.toLocaleString()} rows
            {onGoData && <> · <button className="link" onClick={onGoData}>Edit selection</button></>}
          </span>
        </div>
        {noTables ? (
          <p className="muted">No tables selected. {onGoData && <button className="link" onClick={onGoData}>Select tables in Data Migration</button>}.</p>
        ) : (
          <div className="tablelist">
            {groups.map(([schema, ts]) => (
              <div key={schema} className="tgroup">
                <div className="tgroup__head">
                  <span className="tgroup__title">
                    {schema} <span className="tgroup__arrow">→</span> {mapSchema(schema, state.targetSchema, state.identifierCase)}
                  </span>
                  <span className="tgroup__count">{ts.length}</span>
                </div>
                {ts.map((t) => (
                  <div key={t.table_name} className="trow trow--static">
                    <span className="trow__name">{t.table_name}</span>
                    <span className="streammode">{syncModeLabel}</span>
                    <span className="trow__target">
                      {mapSchema(schema, state.targetSchema, state.identifierCase)}.{mapObject(t.table_name, state.identifierCase)}
                    </span>
                    <span className="trow__rows">{t.row_count.toLocaleString()} rows</span>
                  </div>
                ))}
              </div>
            ))}
          </div>
        )}
      </section>

      {mode === "now" ? (
        <section className="card">
          <div className="card__head">
            <h3>Sync now</h3>
            {!state.plan && <span className="muted">No schema plan — only data will load</span>}
          </div>
          <p className="muted">
            {syncScope === "schema"
              ? "Applies the whole plan to Lakebase — schemas, tables, translated code, constraints & indexes — but loads no data. Load the data later with a full run or Async mode."
              : `Applies the schema & code plan to Lakebase, streams the selected tables (${overwrite ? "truncating targets first" : "appending"}), then creates the constraints, indexes, foreign keys, and triggers once the data is in. Runs here with live progress.`}
          </p>
          <div className="modecards" style={{ marginTop: 14 }}>
            <ModeCard
              on={syncScope === "schema"} onSelect={() => setSyncScope("schema")}
              badge="No data" title="Schema & code only"
              desc="Only apply the migration plan — create the target objects without loading data."
            />
            <ModeCard
              on={syncScope === "full"} onSelect={() => setSyncScope("full")}
              badge="Schema + data" title="Full migration"
              desc="Apply the schema & code plan, then stream the selected tables into Lakebase."
            />
          </div>
          {noTarget && (
            <div className="banner banner--err">
              No Lakebase target configured.{" "}
              {onGoConnection && <button className="link" onClick={onGoConnection}>Set it in Connections &amp; Target</button>}
            </div>
          )}
          {!state.plan && onGoSchema && (
            <div className="banner banner--err">
              No migration plan yet — <button className="link" onClick={onGoSchema}>generate it in Schema &amp; Code</button>
              {syncScope === "schema" ? " to have something to apply." : " to create schema/code (data will still load)."}
            </div>
          )}
          <div className="actions">
            {syncScope === "schema" ? (
              <button className="btn btn--primary" disabled={busy || running || noTarget || !state.plan?.length} onClick={applySchemaOnly}>
                {busy ? "Applying…" : "Apply schema & code"}
              </button>
            ) : (
              <button className="btn btn--primary" disabled={busy || running || noTarget || noTables} onClick={syncNow}>
                {busy || running ? "Syncing…" : "Start sync now"}
              </button>
            )}
          </div>
          <OverallProgress
            busy={busy}
            hasPlan={prePlan.length > 0}
            hasPost={postPlan.length > 0}
            schemaOnly={schemaOnly}
            applyRes={applyRes}
            postApplyRes={postApplyRes}
            postBusy={postBusy}
            run={run}
            error={error}
          />
          {error && <div className="banner banner--err">{error}</div>}
          {applyRes && <ApplySummary res={applyRes} onGoValidation={onGoValidation} />}
          {run && <DataProgress run={run} />}
          {postApplyRes && (
            <ApplySummary res={postApplyRes} label="Constraints & indexes" onGoValidation={onGoValidation} />
          )}
        </section>
      ) : (
        <section className="card">
          <div className="card__head">
            <h3>Async mode — PySpark snapshot</h3>
            <span className="muted">Source → PySpark job → Lakebase (Postgres)</span>
          </div>
          <p className="muted">
            Applies the same schema &amp; code plan as Sync now, then a PySpark job on Databricks
            snapshots the selected tables into Lakebase — source reads are range-partitioned across
            the cluster, several tables load concurrently, and each partition streams straight into
            the plan-created tables with Postgres COPY. The plan's constraints, indexes, foreign
            keys, and triggers are applied by chained job tasks — one per object type — that run
            after the copy succeeds, each with per-statement progress in its own run output. Create
            it to run later on the compute you choose, or run it now.
          </p>

          {workspace && !workspace.connected && (
            <div className="banner banner--err" style={{ marginTop: 14 }}>
              Not connected to a Databricks workspace.{" "}
              {onManageWorkspace && <button className="link" onClick={onManageWorkspace}>Log in in Settings</button>}
            </div>
          )}

          <div className="modecards" style={{ marginTop: 14 }}>
            <ModeCard
              on={schedule === "create"} onSelect={() => setSchedule("create")}
              badge="Create only" title="Create job, run later"
              desc="Create the Databricks job without starting it — open it in the workspace to pick the compute (serverless or a cluster), tune it, and hit Run now."
            />
            <ModeCard
              on={schedule === "once"} onSelect={() => setSchedule("once")}
              badge="One-off" title="Run once"
              desc="Submit a single Databricks job run that snapshots the selected tables into Lakebase now (on serverless compute)."
            />
          </div>

          <div className="field-row" style={{ marginTop: 14 }}>
            <SecretScopeField
              value={secretScope}
              onChange={setSecretScope}
              onResolvedOption={setRuntimeScopeOption}
              label="Runtime secret scope"
              newLabel="Create a new scope…"
            />
            <div className="field"><label>Workspace folder (notebooks)</label><input value={workspaceDir} onChange={(e) => setWorkspaceDir(e.target.value)} /></div>
          </div>
          <div className="field-row">
            <div className="field"><label>Source password secret key</label><input value={pwdKey} onChange={(e) => setPwdKey(e.target.value)} /></div>
            <div className="field"><label>Lakebase password secret key</label><input value={lakebasePwdKey} onChange={(e) => setLakebasePwdKey(e.target.value)} /></div>
          </div>

          <label className="check" style={{ marginTop: 4 }}>
            <input type="checkbox" checked={syncSecrets} onChange={(e) => setSyncSecrets(e.target.checked)} />
            Create / update the runtime scope from the connection credentials on run
          </label>
          {syncSecrets && runtimeScopeOption?.backend_type === "AZURE_KEYVAULT" && (
            <div className="banner banner--err">
              Key Vault-backed scopes are read-only through the Databricks Secrets API. Disable
              credential sync to use keys already present in this scope, or choose/create a
              Databricks-backed runtime scope.
            </div>
          )}
          <div className="banner banner--warn">
            The snapshot writes into the tables the schema &amp; code plan creates, so this reuses that
            plan (applied here first). Choose an existing runtime scope or enter a new name. With the
            box above ticked, the source and Lakebase passwords are resolved from their configured
            password sources and written to the two keys above (created if missing, overwritten if they
            exist); the generated notebook reads both from the runtime scope — never inline them.
          </div>

          {noTarget && (
            <div className="banner banner--err">
              No Lakebase target configured.{" "}
              {onGoConnection && <button className="link" onClick={onGoConnection}>Set it in Connections &amp; Target</button>}
            </div>
          )}
          {!state.plan && onGoSchema && (
            <div className="banner banner--err">
              No migration plan yet — <button className="link" onClick={onGoSchema}>generate it in Schema &amp; Code</button> to create the target tables (the snapshot loads into them).
            </div>
          )}
          <div className="actions">
            <button className="btn" disabled={previewBusy || noTables} onClick={previewNotebooks}>
              {previewBusy ? "Generating…" : "Preview generated notebook"}
            </button>
            <button className="btn btn--primary" disabled={asyncBusy || noTables || noTarget} onClick={setupAsync}>
              {asyncBusy ? "Setting up…"
                : schedule === "create" ? "Apply plan & create job"
                : "Apply plan & run snapshot"}
            </button>
          </div>
          {error && <div className="banner banner--err">{error}</div>}
          {secretMsg && <div className="banner banner--ok">{secretMsg}</div>}
          {applyRes && <ApplySummary res={applyRes} onGoValidation={onGoValidation} />}
          {asyncRes && (
            <div className="banner banner--ok">
              {asyncRes.run_id
                ? `Created snapshot job ${asyncRes.job_id} and submitted run ${asyncRes.run_id}. `
                : asyncRes.job_id
                ? `Created snapshot job ${asyncRes.job_id} — no run started. `
                : `Uploaded ${asyncRes.notebook_paths.length} notebook(s). `}
              {asyncRes.note}{" "}
              {asyncRes.url && <a href={asyncRes.url} target="_blank" rel="noreferrer">Open job ↗</a>}{" "}
              {asyncRes.run_url && <a href={asyncRes.run_url} target="_blank" rel="noreferrer">Open run ↗</a>}
            </div>
          )}
          {preview && (
            <div className="stack" style={{ marginTop: 14 }}>
              {preview.map((a) => (
                <div key={a.filename}>
                  <p className="muted"><strong>{a.name}</strong> — {a.description}</p>
                  <CodeBlock code={a.code} language={a.language} filename={a.filename} />
                </div>
              ))}
            </div>
          )}
        </section>
      )}
    </div>
  );
}

function ModeCard({ on, onSelect, badge, title, desc }: { on: boolean; onSelect: () => void; badge: string; title: string; desc: string }) {
  return (
    <button className={`modecard ${on ? "modecard--on" : ""}`} onClick={onSelect} aria-pressed={on}>
      <span className="modecard__radio" aria-hidden />
      <span className="modecard__body">
        <span className="modecard__title">{title} <span className="modecard__badge">{badge}</span></span>
        <span className="modecard__desc">{desc}</span>
      </span>
    </button>
  );
}

/** A single top-level bar spanning the whole sync-now run: schema & code apply
 *  (~15%), the data load (driven by real rows copied), then the post-data
 *  constraints & indexes apply (~10%). Evolves as the run progresses and
 *  settles green / amber / red on completion. */
function OverallProgress({
  busy,
  hasPlan,
  hasPost = false,
  schemaOnly = false,
  applyRes,
  postApplyRes = null,
  postBusy = false,
  run,
  error,
}: {
  busy: boolean;
  hasPlan: boolean;
  hasPost?: boolean;
  schemaOnly?: boolean;
  applyRes: ApplyResponse | null;
  postApplyRes?: ApplyResponse | null;
  postBusy?: boolean;
  run: RunState | null;
  error: string | null;
}) {
  if (!busy && !applyRes && !run && !error) return null;

  // Schema-&-code-only apply: single phase, settled once the apply returns.
  if (schemaOnly) {
    const tone = error || (applyRes && applyRes.failed)
      ? (applyRes && applyRes.success > 0 ? "warn" : "failed")
      : applyRes ? "success" : "running";
    const label = tone === "running" ? "Applying schema & code…"
      : tone === "success" ? "Schema & code applied"
      : tone === "warn" ? "Schema & code applied with errors"
      : "Schema & code apply failed";
    return <SyncBar pct={tone === "running" ? 45 : 100} tone={tone} label={label} />;
  }

  const SCHEMA_W = hasPlan ? 15 : 0; // % of the bar allotted to the schema phase
  const POST_W = hasPost ? 10 : 0;   // % allotted to the constraints & indexes phase
  const DATA_W = 100 - SCHEMA_W - POST_W;

  // Real data-phase fraction: prefer rows copied, fall back to tables done.
  let dataFrac = 0;
  let tablesLabel = "";
  if (run) {
    const totalRows = run.tables.reduce((n, t) => n + t.total_rows, 0);
    const copied = run.tables.reduce((n, t) => n + t.rows_copied, 0);
    const totalT = run.tables.length;
    const doneT = run.tables.filter((t) => t.status === "success" || t.status === "failed").length;
    dataFrac =
      run.status !== "running" ? 1 : totalRows > 0 ? Math.min(1, copied / totalRows) : totalT > 0 ? doneT / totalT : 0;
    tablesLabel = `${doneT.toLocaleString()} / ${totalT.toLocaleString()} tables`;
  }

  const dataDone = !!run && run.status !== "running";
  // A fully failed data run (setup-level) skips the post phase.
  const postExpected = hasPost && dataDone && run!.status !== "failed";
  const postSettled = !!postApplyRes || !postExpected;

  let pct: number;
  let label: string;
  let tone: "running" | "success" | "warn" | "failed";

  if (dataDone && postSettled) {
    pct = 100;
    const postFailed = postApplyRes?.failed ?? 0;
    tone = run!.status === "success"
      ? (postFailed || error ? "warn" : "success")
      : run!.status === "partial" ? "warn" : "failed";
    label =
      run!.status === "failed" ? "Sync failed"
      : tone === "success" ? "Sync complete"
      : "Sync completed with errors";
  } else if (dataDone && postExpected) {
    // Post-data phase in flight (constraints, indexes, FKs, triggers).
    pct = SCHEMA_W + DATA_W + (postBusy ? POST_W * 0.45 : 0);
    tone = "running";
    label = "Applying constraints & indexes…";
  } else if (error) {
    pct = run ? SCHEMA_W + DATA_W * dataFrac : applyRes ? SCHEMA_W : 4;
    tone = "failed";
    label = "Sync failed";
  } else if (run) {
    pct = SCHEMA_W + DATA_W * dataFrac;
    tone = "running";
    label = `Loading data — ${tablesLabel}`;
  } else if (applyRes) {
    pct = SCHEMA_W;
    tone = "running";
    label = "Schema applied — starting data load…";
  } else {
    // Schema phase in flight (or kicking off) — no sub-progress available yet.
    pct = hasPlan ? Math.max(4, SCHEMA_W * 0.45) : 4;
    tone = "running";
    label = hasPlan ? "Applying schema & code…" : "Starting data load…";
  }

  const running = tone === "running";
  return (
    <SyncBar pct={pct} tone={tone} label={label}>
      {hasPlan && (
        <div className="syncbar__steps">
          <Phase label="Schema & code" done={!!applyRes || !!run} active={running && !applyRes && !run} />
          <Phase
            label="Data"
            done={dataDone && run!.status === "success"}
            active={running && !dataDone && (!!applyRes || !!run)}
          />
          {hasPost && (
            <Phase
              label="Constraints & indexes"
              done={!!postApplyRes}
              active={running && dataDone && !postApplyRes}
            />
          )}
        </div>
      )}
    </SyncBar>
  );
}

function SyncBar({ pct, tone, label, children }: {
  pct: number;
  tone: "running" | "success" | "warn" | "failed";
  label: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="syncbar">
      <div className="syncbar__head">
        <span className="syncbar__label">
          {tone === "running" && <span className="syncbar__spin" aria-hidden />}
          {tone === "success" && <span className="syncbar__check" aria-hidden>✓</span>}
          {label}
        </span>
        <span className="syncbar__pct">{Math.round(pct)}%</span>
      </div>
      <div className="bar syncbar__track">
        <div
          className={`bar__fill syncbar__fill syncbar__fill--${tone}`}
          style={{ width: `${Math.max(2, pct)}%` }}
        />
      </div>
      {children}
    </div>
  );
}

function Phase({ label, done, active }: { label: string; done: boolean; active: boolean }) {
  return (
    <span className={`syncbar__step ${done ? "is-done" : active ? "is-active" : ""}`}>
      <span className="syncbar__dot" aria-hidden>{done ? "✓" : "○"}</span>
      {label}
    </span>
  );
}

function ApplySummary({ res, label = "Schema & code", onGoValidation }: {
  res: ApplyResponse;
  label?: string;
  onGoValidation?: () => void;
}) {
  const failed = res.results.filter((r) => r.status === "failed");
  return (
    <div className="phasebox">
      <div className="prog__row">
        <span className="prog__name">{label}</span>
        <span className="prog__count">{res.success} applied · {res.failed} failed · {res.skipped} skipped</span>
        <span className={`sbadge sbadge--${res.failed ? "err" : "ok"}`}>{res.failed ? "partial" : "applied"}</span>
      </div>
      {failed.length > 0 && (
        <>
          <div className="findings" style={{ marginTop: 10 }}>
            {failed.map((f) => (
              <div key={f.id} className="finding finding--high">
                <span className="tag tag--high">{f.kind}</span>
                <div className="finding__body">
                  <div className="finding__title"><strong>{f.name}</strong></div>
                  {f.error && <div className="finding__detail">{f.error}</div>}
                </div>
              </div>
            ))}
          </div>
          <p className="muted" style={{ marginTop: 10 }}>
            ✦ After the sync, run{" "}
            {onGoValidation
              ? <button className="link" onClick={onGoValidation}>Post-Migration Validation</button>
              : "Post-Migration Validation"}{" "}
            — its AI repair agent picks up whatever is missing or inconsistent and fixes it there.
          </p>
        </>
      )}
    </div>
  );
}

function DataProgress({ run }: { run: RunState }) {
  const total = run.tables.length;
  const done = run.tables.filter((t) => t.status === "success" || t.status === "failed").length;
  const ok = run.tables.filter((t) => t.status === "success").length;
  const failed = run.tables.filter((t) => t.status === "failed").length;
  const rowsCopied = run.tables.reduce((n, t) => n + t.rows_copied, 0);
  const pct = total > 0 ? (done / total) * 100 : 0;
  const statusCls = run.status === "success" ? "ok" : run.status === "failed" || run.status === "partial" ? "err" : "skip";

  return (
    <div className="phasebox">
      <div className="prog__row">
        <span className="prog__name">
          Data · {done.toLocaleString()} / {total.toLocaleString()} tables
          {ok > 0 ? ` · ${ok.toLocaleString()} ok` : ""}{failed > 0 ? ` · ${failed.toLocaleString()} failed` : ""}
        </span>
        <span className="prog__count">{rowsCopied.toLocaleString()} rows</span>
        <span className={`sbadge sbadge--${statusCls}`}>{run.status}</span>
      </div>
      <div className="bar">
        <div className={`bar__fill bar__fill--${run.status === "running" ? "running" : statusCls === "ok" ? "success" : "failed"}`} style={{ width: `${pct}%` }} />
      </div>
      {run.error && <div className="banner banner--err">{run.error}</div>}
      <div className="proglist" style={{ marginTop: 12 }}>
        {run.tables.map((tp) => {
          const tpct = tp.total_rows > 0 ? Math.min(100, (tp.rows_copied / tp.total_rows) * 100) : tp.status === "success" ? 100 : 0;
          return (
            <div key={tp.name} className="prog">
              <div className="prog__row">
                <span className="prog__name">{tp.name} <span className="trow__target">{tp.target}</span></span>
                <span className="prog__count">{tp.rows_copied.toLocaleString()}{tp.total_rows ? ` / ${tp.total_rows.toLocaleString()}` : ""} rows</span>
                <span className={`sbadge sbadge--${tp.status === "success" ? "ok" : tp.status === "failed" ? "err" : "skip"}`}>{tp.status}</span>
              </div>
              <div className="bar"><div className={`bar__fill bar__fill--${tp.status}`} style={{ width: `${tpct}%` }} /></div>
              {tp.error && <div className="banner banner--err">{tp.error}</div>}
            </div>
          );
        })}
      </div>
    </div>
  );
}
