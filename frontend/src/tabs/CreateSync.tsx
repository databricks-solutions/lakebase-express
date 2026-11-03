import { useEffect, useMemo, useRef, useState } from "react";
import { api, POST_DATA_KINDS, type ApplyResponse, type AsyncSetupResult, type CapacityLimits, type PlanItem, type ResumableAsyncRun, type ResumableRun, type RunState, type SecretScopeOption, type TableInfo, type TableRef, type TuningOverrides, type TuningPlan, type WorkspaceStatus } from "../api";
import CodeBlock from "../components/CodeBlock";
import SecretScopeField from "../components/SecretScopeField";
import type { MigrationState } from "../App";
import { mapObject, mapSchema } from "../naming";
import { adviseMode, type Mode } from "../modeAdvice";

interface Props {
  state: MigrationState;
  setState: React.Dispatch<React.SetStateAction<MigrationState>>;
  onGoConnection?: () => void;
  onGoSchema?: () => void;
  onGoData?: () => void;
  onGoValidation?: () => void;
  workspace?: WorkspaceStatus | null;
  onManageWorkspace?: () => void;
}

type SyncScope = "schema" | "full";

export default function CreateSync({ state, setState, onGoConnection, onGoSchema, onGoData, onGoValidation, workspace, onManageWorkspace }: Props) {
  const report = state.report;
  const conn = state.connection;

  // Null until the user picks one; until then the size-based recommendation applies.
  const [chosenMode, setMode] = useState<Mode | null>(null);
  const advice = useMemo(
    () => adviseMode(
      (report?.tables ?? []).filter((t) =>
        state.selection.length === 0 || state.selection.includes(`${t.schema_name}.${t.table_name}`)),
      workspace?.connected !== false,
    ),
    [report, state.selection, workspace?.connected],
  );
  const mode: Mode = chosenMode ?? advice?.mode ?? "now";

  // Sync-now execution state.
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [applyRes, setApplyRes] = useState<ApplyResponse | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [run, setRun] = useState<RunState | null>(null);
  // A previous run of this project that can be resumed instead of re-copied.
  const [resumable, setResumable] = useState<ResumableRun | null>(null);
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
  // Create/update the secret scope from the entered connection passwords on submit.
  const [syncSecrets, setSyncSecrets] = useState(true);
  const [secretMsg, setSecretMsg] = useState<string | null>(null);
  const [asyncBusy, setAsyncBusy] = useState(false);
  const [asyncRes, setAsyncRes] = useState<AsyncSetupResult | null>(null);
  // Re-check of the job identity's run-store access, after the user creates its role.
  const [rsCheck, setRsCheck] = useState<{ ok: boolean; warning?: string | null; fix?: string | null } | null>(null);
  const [rsBusy, setRsBusy] = useState(false);
  // A previous job run of this project whose tables can be finished off.
  const [asyncResumable, setAsyncResumable] = useState<ResumableAsyncRun | null>(null);

  // Load tuning: the recommendation for the selected tables, and the probed limits it
  // came from (kept per connection pair, so editing an override doesn't re-probe).
  const [tuningPlan, setTuningPlan] = useState<TuningPlan | null>(null);
  const [tuningBusy, setTuningBusy] = useState(false);
  const [tuningError, setTuningError] = useState<string | null>(null);
  const [probeNonce, setProbeNonce] = useState(0);
  const capacityRef = useRef<{ key: string; limits: CapacityLimits } | null>(null);
  const limitsKey = `${conn?.host}/${conn?.database}|${state.lakebase?.host}/${state.lakebase?.database}`;
  const tuningRefs = useMemo(
    () => tableRefs(state.report?.tables ?? [], state.selection),
    [state.report, state.selection],
  );
  const tuningKey = JSON.stringify(state.tuning);

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

  // Offer a resume when an earlier run's loader is gone and tables are still left.
  // Re-checked whenever a run settles, since finishing one clears the offer.
  useEffect(() => {
    const projectId = conn?.project_id;
    if (!projectId) { setResumable(null); return; }
    let live = true;
    api.resumableData(projectId)
      .then((r) => { if (live) setResumable(r.run); })
      .catch(() => {});
    return () => { live = false; };
  }, [conn?.project_id, run?.status]);

  // The same offer for async mode, where the loader notebook wrote the checkpoints.
  // Re-checked after a provision: the run it offered is superseded by the new one.
  useEffect(() => {
    const projectId = conn?.project_id;
    if (!projectId || mode !== "async") { setAsyncResumable(null); return; }
    let live = true;
    api.resumableAsync(projectId)
      .then((r) => { if (live) setAsyncResumable(r.run); })
      .catch(() => {});
    return () => { live = false; };
  }, [conn?.project_id, mode, asyncRes?.run_id]);

  useEffect(() => {
    if (mode !== "async" || !tuningRefs.length) return;
    const cached = capacityRef.current?.key === limitsKey ? capacityRef.current.limits : null;
    let live = true;
    const timer = setTimeout(() => {
      setTuningBusy(true);
      api.recommendTuning({
        tables: tuningRefs, overrides: state.tuning, capacity: cached,
        source: cached ? null : conn, lakebase: cached ? null : state.lakebase,
      })
        .then((r) => {
          capacityRef.current = { key: limitsKey, limits: r.capacity };
          if (live) { setTuningPlan(r.plan); setTuningError(null); }
        })
        .catch((e) => { if (live) setTuningError((e as Error).message); })
        .finally(() => { if (live) setTuningBusy(false); });
    }, cached ? 300 : 0);
    return () => { live = false; clearTimeout(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, tuningRefs, tuningKey, limitsKey, probeNonce]);

  function recheckLimits() {
    capacityRef.current = null;
    setProbeNonce((n) => n + 1);
  }

  const setTuning = (patch: TuningOverrides) =>
    setState((s) => ({ ...s, tuning: { ...s.tuning, ...patch } }));
  // Clears every load override (and any legacy field); translation workers belong to Schema & Code.
  const resetTuning = () =>
    setState((s) => ({ ...s, tuning: { translate_workers: s.tuning.translate_workers ?? null } }));

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

  const byGroup: Record<string, TableInfo[]> = {};
  for (const t of selectedTables) (byGroup[t.schema_name] ??= []).push(t);
  const groups = Object.entries(byGroup).sort(([a], [b]) => a.localeCompare(b));

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

  // ``resumeFrom`` continues that run: the tables it loaded are skipped, not re-copied.
  async function syncNow(resumeFrom?: string) {
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
        resume_from: resumeFrom ?? null,
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
      project_id: conn!.project_id,
      host: conn!.host, database: conn!.database, username: conn!.username,
      password_secret_key: pwdKey, secret_scope: secretScope, port: conn!.port,
      target_schema: state.targetSchema,
      identifier_case: state.identifierCase,
      lakebase_host: state.lakebase?.host ?? "",
      lakebase_port: state.lakebase?.port ?? 5432,
      lakebase_database: state.lakebase?.database ?? "lakebase",
      lakebase_user: state.lakebase?.user ?? "",
      lakebase_password_secret_key: lakebasePwdKey,
      // primary_key feeds the range-partitioned (parallel) source reads; the stats size them.
      tables: tuningRefs,
      capacity: capacityRef.current?.key === limitsKey ? capacityRef.current.limits : null,
      tuning: state.tuning,
      // Post-data plan items (constraints/indexes/FKs/triggers) — the job
      // applies them itself after the snapshot, since it may run detached
      // from the app. `kind` lets the job split them into one task per type.
      // User edits to the plan SQL ride along verbatim.
      post_load_sql: postPlan
        .filter((it) => it.sql.trim())
        .map((it) => ({ name: it.name, sql: it.sql, kind: it.kind })),
    };
  }

  // ``resumeFrom`` continues that job run: the notebook skips the tables it loaded.
  async function setupAsync(resumeFrom?: string) {
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
        // A resume is a run: there is nothing to continue in a job left unstarted.
        run_now: !!resumeFrom,
        resume_from: resumeFrom ?? null,
      });
      setAsyncRes(r);
      setRsCheck(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setAsyncBusy(false);
    }
  }

  // The verdict shown is the latest one: a re-check supersedes what provisioning said.
  const runStateWarning = rsCheck ? (rsCheck.ok ? null : rsCheck.warning) : asyncRes?.run_state_warning;
  const runStateFix = rsCheck ? (rsCheck.ok ? null : rsCheck.fix) : asyncRes?.run_state_fix;

  async function recheckRunState() {
    if (!asyncRes?.job_id) return;
    setRsBusy(true); setError(null);
    try {
      setRsCheck(await api.runStateAccess({ job_id: asyncRes.job_id }));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRsBusy(false);
    }
  }

  const plural = (n: number, word: string) => `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;
  const kvScope = syncSecrets && runtimeScopeOption?.backend_type === "AZURE_KEYVAULT";

  return (
    <div className="stack">
      <section className="card">
        <h2>How do you want to run it?</h2>
        <div className="modecards">
          <ModeCard
            on={mode === "now"} onSelect={() => setMode("now")} badge="In-app" title="Sync now"
            recommended={advice?.mode === "now"}
            desc="Runs in this app with live progress. Good for small and medium databases."
          />
          <ModeCard
            on={mode === "async"} onSelect={() => setMode("async")} badge="Databricks job" title="Async mode"
            recommended={advice?.mode === "async"}
            desc="Creates a parallel PySpark job you run on your own compute. Built for large databases."
          />
        </div>
        {advice && (
          <p className="muted modeadvice">
            <strong>{advice.mode === "async" ? "Async mode" : "Sync now"} recommended:</strong> {advice.reason}.
          </p>
        )}
      </section>

      <section className="card">
        <div className="card__head">
          <h3>Tables to migrate</h3>
          <span className="muted">
            {plural(selectedTables.length, "table")} · {selectedRows.toLocaleString()} rows · {syncModeLabel}
            {onGoData && <> · <button className="link" onClick={onGoData}>Edit selection</button></>}
          </span>
        </div>
        {noTables ? (
          <p className="muted">No tables selected. {onGoData && <button className="link" onClick={onGoData}>Select tables in Data Migration</button>}.</p>
        ) : (
          <details className="orig">
            <summary>Show tables and their Lakebase targets</summary>
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
                      <span className="trow__target">
                        {mapSchema(schema, state.targetSchema, state.identifierCase)}.{mapObject(t.table_name, state.identifierCase)}
                      </span>
                      <span className="trow__rows">{t.row_count.toLocaleString()} rows</span>
                    </div>
                  ))}
                </div>
              ))}
            </div>
          </details>
        )}
      </section>

      {mode === "now" ? (
        <section className="card">
          <div className="card__head">
            <h3>Sync now</h3>
            <div className="segmented" role="tablist" aria-label="What to sync" style={{ marginBottom: 0 }}>
              {([["schema", "Schema & code only"], ["full", "Full migration"]] as const).map(([v, label]) => (
                <button
                  key={v} type="button" role="tab" aria-selected={syncScope === v}
                  className={`segmented__btn ${syncScope === v ? "segmented__btn--on" : ""}`}
                  onClick={() => setSyncScope(v)}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
          <p className="muted">
            {syncScope === "schema"
              ? "Creates the schemas, tables, code, constraints and indexes in Lakebase. No data is copied."
              : `Creates the schema and code, copies the selected tables (${overwrite ? "replacing" : "appending to"} existing rows), then adds constraints and indexes.`}
          </p>
          {noTarget && <NoTarget onGoConnection={onGoConnection} />}
          {!state.plan && onGoSchema && (
            <div className="banner banner--err">
              No migration plan yet. <button className="link" onClick={onGoSchema}>Generate it in Schema &amp; Code</button>
              {syncScope === "schema" ? "." : " (data can still load without it)."}
            </div>
          )}
          {resumable && syncScope === "full" && (
            <ResumeOffer left={resumable.tables_left} total={resumable.tables_total} rows={resumable.rows_copied} />
          )}
          <div className="actions">
            {syncScope === "schema" ? (
              <button className="btn btn--primary" disabled={busy || running || noTarget || !state.plan?.length} onClick={applySchemaOnly}>
                {busy ? "Applying…" : "Apply schema & code"}
              </button>
            ) : (
              <>
                <button className="btn btn--primary" disabled={busy || running || noTarget || noTables} onClick={() => syncNow()}>
                  {busy || running ? "Syncing…" : "Start sync now"}
                </button>
                {resumable && (
                  <button className="btn" disabled={busy || running || noTarget || noTables} onClick={() => syncNow(resumable.run_id)}>
                    Resume last run
                  </button>
                )}
              </>
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
            <h3 style={{ display: "flex", alignItems: "center" }}>
              Async mode
              <InfoTip label="How the job works">
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  <li>The schema &amp; code plan is applied here first, so the target tables exist.</li>
                  <li>Source reads are range-partitioned on the primary key, and several tables load at once.</li>
                  <li>Each partition streams into Lakebase with Postgres COPY — no Delta landing in between.</li>
                  <li>After the copy, one chained task per object type adds constraints, indexes, foreign keys and triggers.</li>
                  <li>Passwords are read from a Databricks secret scope at runtime, never written into the notebook.</li>
                </ul>
              </InfoTip>
            </h3>
            <span className="muted">Source → PySpark job → Lakebase</span>
          </div>
          <p className="muted">
            Creates a Databricks job that copies the selected tables in parallel, then adds
            constraints, indexes, foreign keys and triggers. Open the job in your workspace to pick
            the compute and run it.
          </p>

          {workspace && !workspace.connected && (
            <div className="banner banner--err">
              Not connected to a Databricks workspace.{" "}
              {onManageWorkspace && <button className="link" onClick={onManageWorkspace}>Log in in Settings</button>}
            </div>
          )}
          {noTarget && <NoTarget onGoConnection={onGoConnection} />}
          {!state.plan && onGoSchema && (
            <div className="banner banner--err">
              No migration plan yet. <button className="link" onClick={onGoSchema}>Generate it in Schema &amp; Code</button> — the job loads into the tables it creates.
            </div>
          )}

          <div className="stack" style={{ marginTop: 14 }}>
            <Collapsible title="Job settings" meta={`${secretScope} · ${workspaceDir}`}>
              <div className="field-row">
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
              <label className="check">
                <input type="checkbox" checked={syncSecrets} onChange={(e) => setSyncSecrets(e.target.checked)} />
                Write the connection passwords to these keys when creating the job
              </label>
              <p className="muted" style={{ margin: 0, fontSize: 13 }}>
                Keys are created if missing and overwritten if they exist. Untick to use keys already in the scope.
              </p>
            </Collapsible>
            {kvScope && (
              <div className="banner banner--err" style={{ marginTop: 0 }}>
                Key Vault-backed scopes are read-only through the Databricks Secrets API. Untick
                password sync in Job settings to use existing keys, or pick a Databricks-backed scope.
              </div>
            )}
            {!noTables && (
              <LoadTuning
                plan={tuningPlan} overrides={state.tuning} busy={tuningBusy} error={tuningError}
                onChange={setTuning} onReset={resetTuning} onRecheck={recheckLimits}
              />
            )}
          </div>

          {asyncResumable && (
            <ResumeOffer
              left={asyncResumable.tables_left} total={asyncResumable.tables_total}
              rows={asyncResumable.rows_copied} runUrl={asyncResumable.run_url}
            />
          )}
          <div className="actions">
            <button className="btn btn--primary" disabled={asyncBusy || noTables || noTarget || kvScope} onClick={() => setupAsync()}>
              {asyncBusy ? "Setting up…" : "Apply plan & create job"}
            </button>
            {asyncResumable && (
              <button className="btn" disabled={asyncBusy || noTables || noTarget || kvScope} onClick={() => setupAsync(asyncResumable.run_id)}>
                Resume last run
              </button>
            )}
          </div>
          {error && <div className="banner banner--err">{error}</div>}
          {asyncRes && (
            <div className="banner banner--ok">
              {asyncRes.run_id
                ? <>Job {asyncRes.job_id} created and a resume run started — only the unfinished tables are copied.</>
                : <>Job {asyncRes.job_id} created. Open it, pick the compute, and click <strong>Run now</strong>.</>}{" "}
              {asyncRes.url && <a href={asyncRes.url} target="_blank" rel="noreferrer">Open job ↗</a>}{" "}
              {asyncRes.run_url && <a href={asyncRes.run_url} target="_blank" rel="noreferrer">Open run ↗</a>}
              {secretMsg && <div style={{ marginTop: 4, fontSize: 13 }}>{secretMsg}</div>}
            </div>
          )}
          {applyRes && <ApplySummary res={applyRes} onGoValidation={onGoValidation} />}
          {asyncRes && rsCheck?.ok && (
            <div className="banner banner--ok">
              Run-state access confirmed — this job will record its progress in run history.
            </div>
          )}
          {runStateWarning && (
            <div className="banner banner--warn">{runStateWarning}</div>
          )}
          {runStateFix && (
            <CodeBlock
              code={runStateFix} language="sql" filename="grant-run-state-access.sql"
              action={
                <button className="btn btn--sm" disabled={rsBusy} onClick={recheckRunState}>
                  {rsBusy ? "Re-validating…" : "Re-validate"}
                </button>
              }
            />
          )}
        </section>
      )}
    </div>
  );
}

function NoTarget({ onGoConnection }: { onGoConnection?: () => void }) {
  return (
    <div className="banner banner--err">
      No Lakebase target configured.{" "}
      {onGoConnection && <button className="link" onClick={onGoConnection}>Set it in Connections &amp; Target</button>}
    </div>
  );
}

function ResumeOffer({ left, total, rows, runUrl }: { left: number; total: number; rows: number; runUrl?: string | null }) {
  return (
    <div className="banner banner--info">
      The last run stopped with {left.toLocaleString()} of {total.toLocaleString()} table{total === 1 ? "" : "s"} left
      ({rows.toLocaleString()} rows already copied). <strong>Resume</strong> copies only those;
      starting again copies every table.{" "}
      {runUrl && <a href={runUrl} target="_blank" rel="noreferrer">Open that run ↗</a>}
    </div>
  );
}

/** An ⓘ button whose popover opens on hover or keyboard focus. */
function InfoTip({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <span className="infotip">
      <button type="button" className="infotip__btn" aria-label={label}>i</button>
      <span className="infotip__pop" role="tooltip">{children}</span>
    </span>
  );
}

/** A bordered, collapsed-by-default section with a one-line summary. */
function Collapsible({ title, meta, badge, children }: {
  title: string;
  meta?: React.ReactNode;
  badge?: React.ReactNode;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <details className="planitem" open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary>
        <span className="planitem__name">{title}</span>
        {meta && <span className="muted" style={{ fontSize: 13 }}>{meta}</span>}
        {badge}
        <svg className={`acc__chevron ${open ? "acc__chevron--open" : ""}`} viewBox="0 0 24 24"
             width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
          <path d="m6 9 6 6 6-6" />
        </svg>
      </summary>
      <div className="planitem__body stack">{children}</div>
    </details>
  );
}

/** The selected tables with the stats the tuning recommender sizes them from. */
function tableRefs(tables: TableInfo[], selection: string[]): TableRef[] {
  return tables
    .filter((t) => selection.length === 0 || selection.includes(`${t.schema_name}.${t.table_name}`))
    .map((t) => ({
      schema_name: t.schema_name, table_name: t.table_name, primary_key: t.primary_key ?? [],
      row_count: t.row_count, data_bytes: t.data_bytes ?? 0,
      primary_key_type: t.columns.find((c) => c.name === t.primary_key?.[0])?.data_type ?? "",
    }));
}

const TUNING_ROWS_SHOWN = 50;

// Rows per fetch is picked from these steps; smaller ones only add round trips.
const FETCH_STEPS = [1000, 2500, 5000, 10000, 25000, 50000];

/** Recommended load tuning with per-field overrides; a blank field uses the recommendation. */
function LoadTuning({ plan, overrides, busy, error, onChange, onReset, onRecheck }: {
  plan: TuningPlan | null;
  overrides: TuningOverrides;
  busy: boolean;
  error: string | null;
  onChange: (patch: TuningOverrides) => void;
  onReset: () => void;
  onRecheck: () => void;
}) {
  const num = (v: string, max: number) =>
    v.trim() === "" ? null : Math.min(max, Math.max(1, Math.floor(Number(v)) || 1));
  const overridden = overrides.max_streams != null || overrides.table_streams != null
    || overrides.fetch_size != null || "max_parallel_tables" in overrides || "num_partitions" in overrides;
  return (
    <>
      <Collapsible
        title="Load tuning"
        meta={busy ? "Checking limits…" : plan
          ? `${plan.max_streams} streams · up to ${plan.max_parallel_tables} tables at once`
          : "Recommended from table sizes and database limits"}
        badge={
          <span className={`sbadge ${overridden ? "sbadge--info" : "sbadge--ok"}`}>
            {overridden ? "Customized" : "Recommended"}
          </span>
        }
      >
        <span className="muted" style={{ fontSize: 13 }}>
          Recommended from the table sizes and both databases' limits
          {overridden && <>{" · "}<button className="link" onClick={onReset}>Reset to recommended</button></>}
          {" · "}<button className="link" disabled={busy} onClick={onRecheck}>Re-check limits</button>
        </span>
        <div className="field-row">
          <div className="field">
            <label>Concurrent streams</label>
            <input
              type="number" min={1} max={256} placeholder={plan ? String(plan.max_streams) : "auto"}
              value={overrides.max_streams ?? ""}
              onChange={(e) => onChange({ max_streams: num(e.target.value, 256) })}
            />
          </div>
          <div className="field">
            <label>Streams per table</label>
            <input
              type="number" min={1} max={64} placeholder="per table"
              value={overrides.table_streams ?? ""}
              onChange={(e) => onChange({ table_streams: num(e.target.value, 64) })}
            />
          </div>
          <div className="field">
            <label>Rows per fetch</label>
            <select
              value={overrides.fetch_size ?? ""}
              onChange={(e) => onChange({ fetch_size: e.target.value ? Number(e.target.value) : null })}
            >
              <option value="">Per table (recommended)</option>
              {/* A value saved before these steps existed stays selectable. */}
              {overrides.fetch_size != null && !FETCH_STEPS.includes(overrides.fetch_size) && (
                <option value={overrides.fetch_size}>{overrides.fetch_size.toLocaleString()}</option>
              )}
              {FETCH_STEPS.map((n) => <option key={n} value={n}>{n.toLocaleString()}</option>)}
            </select>
          </div>
        </div>
        <p className="muted" style={{ margin: 0 }}>
          Blank fields use the recommendation. Each stream holds one source and one Lakebase
          connection. The values are written into the notebook, where you can still edit them.
        </p>
        {plan && plan.reasons.length > 0 && (
          <ul className="muted" style={{ margin: 0, paddingLeft: 18 }}>
            {plan.reasons.map((r) => <li key={r}>{r}</li>)}
          </ul>
        )}
        {plan && (
          <details className="orig">
            <summary>Per-table plan ({plan.tables.length.toLocaleString()} tables, largest first)</summary>
            <table className="dtable" style={{ marginTop: 8 }}>
              <thead><tr><th>Table</th><th>Chunks</th><th>Streams</th><th>Rows per fetch</th><th>Why</th></tr></thead>
              <tbody>
                {plan.tables.slice(0, TUNING_ROWS_SHOWN).map((t) => (
                  <tr key={`${t.schema_name}.${t.table_name}`}>
                    <td>{t.schema_name}.{t.table_name}</td>
                    <td>{t.chunks.toLocaleString()}</td>
                    <td>{t.streams}</td>
                    <td>{t.fetch_size.toLocaleString()}</td>
                    <td className="dtable__sub">{t.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {plan.tables.length > TUNING_ROWS_SHOWN && (
              <p className="muted">…and {(plan.tables.length - TUNING_ROWS_SHOWN).toLocaleString()} smaller tables.</p>
            )}
          </details>
        )}
      </Collapsible>
      {/* Kept outside the collapsed panel so problems are never hidden. */}
      {error && <div className="banner banner--err" style={{ marginTop: 0 }}>{error}</div>}
      {plan?.warnings.map((w) => <div key={w} className="banner banner--warn" style={{ marginTop: 0 }}>{w}</div>)}
    </>
  );
}

function ModeCard({ on, onSelect, badge, title, desc, recommended = false }: {
  on: boolean; onSelect: () => void; badge: string; title: string; desc: string; recommended?: boolean;
}) {
  return (
    <button className={`modecard ${on ? "modecard--on" : ""}`} onClick={onSelect} aria-pressed={on}>
      <span className="modecard__radio" aria-hidden />
      <span className="modecard__body">
        <span className="modecard__title">
          {title} <span className="modecard__badge">{badge}</span>
          {recommended && <span className="modecard__badge modecard__badge--rec">Recommended</span>}
        </span>
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
    const doneT = run.tables.filter((t) => t.status !== "pending" && t.status !== "running").length;
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
  const done = run.tables.filter((t) => t.status !== "pending" && t.status !== "running").length;
  const ok = run.tables.filter((t) => t.status === "success").length;
  const failedTables = run.tables.filter((t) => t.status === "failed");
  const failed = failedTables.length;
  // Loaded by the run this one resumed, so not copied again.
  const skipped = run.tables.filter((t) => t.status === "skipped").length;
  const rowsCopied = run.tables.reduce((n, t) => n + t.rows_copied, 0);
  const pct = total > 0 ? (done / total) * 100 : 0;
  const statusCls = run.status === "success" ? "ok" : run.status === "failed" || run.status === "partial" ? "err" : "skip";

  return (
    <div className="phasebox">
      <div className="prog__row">
        <span className="prog__name">
          Data · {done.toLocaleString()} / {total.toLocaleString()} tables
          {ok > 0 ? ` · ${ok.toLocaleString()} ok` : ""}{skipped > 0 ? ` · ${skipped.toLocaleString()} already loaded` : ""}{failed > 0 ? ` · ${failed.toLocaleString()} failed` : ""}
        </span>
        <span className="prog__count">{rowsCopied.toLocaleString()} rows</span>
        <span className={`sbadge sbadge--${statusCls}`}>{run.status}</span>
      </div>
      <div className="bar">
        <div className={`bar__fill bar__fill--${run.status === "running" ? "running" : statusCls === "ok" ? "success" : "failed"}`} style={{ width: `${pct}%` }} />
      </div>
      {run.error && <div className="banner banner--err">{run.error}</div>}
      {/* Failures stay in view; the full per-table list is one click away. */}
      {failedTables.length > 0 && (
        <div className="proglist" style={{ marginTop: 12 }}>
          {failedTables.map((tp) => <TableProgress key={tp.name} tp={tp} />)}
        </div>
      )}
      <details className="orig">
        <summary>Per-table progress ({total.toLocaleString()} tables)</summary>
        <div className="proglist" style={{ marginTop: 8 }}>
          {run.tables.map((tp) => <TableProgress key={tp.name} tp={tp} />)}
        </div>
      </details>
    </div>
  );
}

function TableProgress({ tp }: { tp: RunState["tables"][number] }) {
  const pct = tp.total_rows > 0 ? Math.min(100, (tp.rows_copied / tp.total_rows) * 100) : tp.status === "success" ? 100 : 0;
  return (
    <div className="prog">
      <div className="prog__row">
        <span className="prog__name">{tp.name} <span className="trow__target">{tp.target}</span></span>
        <span className="prog__count">{tp.rows_copied.toLocaleString()}{tp.total_rows ? ` / ${tp.total_rows.toLocaleString()}` : ""} rows</span>
        <span className={`sbadge sbadge--${tp.status === "success" ? "ok" : tp.status === "failed" ? "err" : "skip"}`}>{tp.status}</span>
      </div>
      <div className="bar"><div className={`bar__fill bar__fill--${tp.status}`} style={{ width: `${pct}%` }} /></div>
      {tp.error && <div className="banner banner--err">{tp.error}</div>}
      {tp.warning && <div className="banner banner--warn">{tp.warning}</div>}
    </div>
  );
}
