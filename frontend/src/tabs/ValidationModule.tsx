import { useEffect, useMemo, useRef, useState } from "react";
import { api, type MatchStatus, type ObjectDiff, type ObjectKind, type RepairState, type RunState, type ValidationItem, type ValidationRunState } from "../api";
import type { MigrationState } from "../App";

interface Props {
  state: MigrationState;
  setState: React.Dispatch<React.SetStateAction<MigrationState>>;
  goConnection: () => void;
  fmEndpoint?: string;
}

// Post-data kinds (constraint/index/foreign_key) come last, mirroring the order
// the migration creates them in. They are compared as one rollup row per table
// per kind rather than one row per object — a database can have hundreds.
const KIND_ORDER: ObjectKind[] = [
  "schema", "table", "view", "function", "procedure", "trigger",
  "constraint", "index", "foreign_key",
];
const KIND_LABEL: Record<ObjectKind, string> = {
  schema: "Schemas", table: "Tables", view: "Views",
  function: "Functions", procedure: "Procedures", trigger: "Triggers",
  constraint: "Constraints", index: "Indexes", foreign_key: "Foreign keys",
};

const STATUS_META: Record<MatchStatus, { label: string; badge: string }> = {
  matched: { label: "matched", badge: "ok" },
  missing: { label: "missing in target", badge: "err" },
  mismatch: { label: "mismatch", badge: "warn" },
  extra: { label: "extra in target", badge: "skip" },
};
const STATUS_ORDER: MatchStatus[] = ["missing", "mismatch", "extra", "matched"];

// A matched item can still carry a note worth reading — chiefly the trigger
// functions PostgreSQL requires that SQL Server has no equivalent for (backend
// tags these with a "trigger-fn:" id). Flag them so the user sees there's an
// explanation without having to expand the row.
const isNoteworthyMatch = (i: ValidationItem) =>
  i.status === "matched" && i.id.startsWith("trigger-fn:") && !!i.detail;

// Constraints, indexes, and foreign keys are compared per table, as a count with
// a per-object breakdown (backend/validation/models.ObjectDiff) — not one report
// row per object, which would bury everything else on a real database.
const ROLLUP_KINDS: ReadonlySet<ObjectKind> = new Set<ObjectKind>([
  "constraint", "index", "foreign_key",
]);
const isRollup = (i: ValidationItem): i is ValidationItem & { objects: ObjectDiff[] } =>
  ROLLUP_KINDS.has(i.kind) && Array.isArray(i.objects);

type Tone = "ok" | "warn" | "err";
const VERDICT: Record<Tone, { title: string; desc: string }> = {
  ok: { title: "In sync", desc: "Source and Lakebase agree — every compared object exists and matches." },
  warn: { title: "Needs attention", desc: "Most objects match, but some inconsistencies need review or remediation." },
  err: { title: "Out of sync", desc: "Significant gaps between source and Lakebase — remediate below, then re-run." },
};

/** Post-Migration Validation: an independent module that re-scans both sides,
 *  shows how source and Lakebase line up (existence, structure, row counts),
 *  and lets the user remediate each inconsistency — with AI or manually. */
export default function ValidationModule({ state, setState, goConnection, fmEndpoint }: Props) {
  const conn = state.connection;
  const report = state.validation;
  const hasConn = !!conn?.host && !!state.lakebase?.host;

  const [runId, setRunId] = useState<string | null>(null);
  const [run, setRun] = useState<ValidationRunState | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Default on: huge tables are counted by estimate to avoid a slow full scan.
  // Off = exact COUNT(*) on every table (precise, but can take minutes).
  const [useEstimates, setUseEstimates] = useState(true);
  const pollRef = useRef(true);
  // Held in a ref so the poll effect can save the report without taking
  // `setState` as a dependency — see the effect below.
  const setStateRef = useRef(setState);
  setStateRef.current = setState;

  // The LLM behind the AI remediation on this page: the Settings override, or
  // the server's configured default when no override is set.
  const [defaultEndpoint, setDefaultEndpoint] = useState("");
  useEffect(() => { api.listFmEndpoints().then((r) => setDefaultEndpoint(r.default)).catch(() => {}); }, []);
  const llm = fmEndpoint || defaultEndpoint;

  const running = run?.status === "running";
  // Passwords may be blank after a page reload — the backend falls back to its
  // session credential cache and returns a clear error when it has nothing.

  // Poll until the run stops. Depends on runId ALONE: `setState` is rebuilt on
  // every App render, and saving the finished report through it re-renders — so
  // including it here re-ran this effect, which started a second poll loop that
  // saved again, and so on. The loops accumulated and kept hammering
  // /api/validation/status forever after the run had finished.
  useEffect(() => {
    if (!runId) return;
    pollRef.current = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      try {
        const r = await api.validationStatus(runId);
        if (!pollRef.current) return;
        setRun(r);
        if (r.status === "running") timer = setTimeout(tick, 1000);
        else if (r.status === "success" && r.report) {
          setStateRef.current((s) => ({ ...s, validation: r.report! }));
        }
      } catch (e) {
        if (pollRef.current) setError((e as Error).message);
      }
    };
    tick();
    // Stop the in-flight tick from scheduling, and drop any pending timer, so a
    // remount (or unmount mid-run) can't leave an orphaned loop behind.
    return () => {
      pollRef.current = false;
      if (timer) clearTimeout(timer);
    };
  }, [runId]);

  async function start(scope: "full" | "objects" = "full") {
    setError(null);
    setRun(null);
    try {
      const { run_id } = await api.startValidation({
        source: conn!,
        lakebase: state.lakebase!,
        target_schema: state.targetSchema,
        identifier_case: state.identifierCase,
        scope,
        use_estimates: useEstimates,
        // The objects re-check merges into the current report server-side, so
        // table structure and row counts survive without re-counting.
        previous: scope === "objects" ? report : undefined,
      });
      setRunId(run_id);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  return (
    <div className="stack">
      {!hasConn && (
        <div className="card">
          <h2>Connections not configured</h2>
          <p className="muted">Validation compares the live source with the Lakebase target — set up and test both connections first.</p>
          <div className="actions"><button className="btn btn--primary" onClick={goConnection}>Go to Connections &amp; Target</button></div>
        </div>
      )}

      <section className="card">
        <div className="card__head">
          <h2>Source ⇄ Lakebase comparison</h2>
          {report && <span className="muted">last run {new Date(report.generated_at).toLocaleString()}</span>}
        </div>
        <p className="muted">
          Re-scans both sides live and matches every schema, table, view, procedure, function, and
          trigger through the migration's naming rules — checking existence, table structure, and{" "}
          <strong>row counts</strong>. Each inconsistency can be remediated below, with AI or
          manually, without re-running the migration.
        </p>
        <div className="actions">
          <button className="btn btn--primary" disabled={!hasConn || running} onClick={() => start()}>
            {running ? "Validating…" : report ? "Re-run validation" : "Run validation"}
          </button>
        </div>
        <label className="valopt">
          <input
            type="checkbox"
            checked={useEstimates}
            disabled={running}
            onChange={(e) => setUseEstimates(e.target.checked)}
          />
          <span>
            Estimate row counts for very large tables{" "}
            <span className="valopt__hint">
              {useEstimates
                ? "(faster — tables over 5M rows use planner statistics instead of an exact scan)"
                : "(off — every table gets an exact COUNT(*); a very large table can take several minutes)"}
            </span>
          </span>
        </label>
        {run && (running || run.status === "failed") && <RunProgress run={run} />}
        {error && <div className="banner banner--err">{error}</div>}
      </section>

      {report && !running && (
        <>
          <ReportHero report={report} />
          <RepairAgentCard
            state={state}
            setState={setState}
            fmEndpoint={fmEndpoint}
            llm={llm}
            onFixesApplied={() => start("objects")}
          />
          <MatchList state={state} setState={setState} fmEndpoint={fmEndpoint} />
        </>
      )}
    </div>
  );
}

// --- Run progress ------------------------------------------------------------------

function RunProgress({ run }: { run: ValidationRunState }) {
  const counting = run.tables_total > 0;
  const pct =
    run.status !== "running" ? 100
    : run.phase === "Scanning source" ? 12
    : run.phase === "Scanning Lakebase" ? 25
    : counting ? 30 + (run.tables_done / Math.max(1, run.tables_total)) * 65
    : 97;
  const failed = run.status === "failed";
  return (
    <div className="phasebox">
      <div className="prog__row">
        <span className="prog__name">{run.phase}</span>
        <span className="prog__count">{counting ? `${run.tables_done} / ${run.tables_total} tables` : ""}</span>
        <span className={`sbadge sbadge--${failed ? "err" : "ai"}`}>{failed ? "failed" : "running"}</span>
      </div>
      <div className="bar">
        <div className={`bar__fill bar__fill--${failed ? "failed" : "running"}`} style={{ width: `${pct}%` }} />
      </div>
      {run.current && !failed && <p className="muted" style={{ margin: "8px 0 0" }}>Counting {run.current}…</p>}
      {run.error && <div className="banner banner--err">{run.error}</div>}
    </div>
  );
}

// --- Report hero (score + stats) ----------------------------------------------------

function ReportHero({ report }: { report: NonNullable<MigrationState["validation"]> }) {
  // The score is the floored matched percentage, so 100 means literally every
  // object matches — only then does the "In sync" verdict hold.
  const tone: Tone = report.match_score === 100 ? "ok" : report.match_score >= 70 ? "warn" : "err";
  const verdict = VERDICT[tone];
  return (
    <section className="card report-hero">
      <MatchDonut value={report.match_score} tone={tone} />
      <div className="report-hero__main">
        <span className={`verdict-chip verdict-chip--${tone}`}>{verdict.title}</span>
        <p className="muted report-hero__desc">{verdict.desc}</p>
        <div className="statgrid statgrid--4">
          <ValStat value={report.matched} label="Matched" />
          <ValStat value={report.missing} label="Missing in target" alert={report.missing > 0} />
          <ValStat value={report.mismatched} label="Mismatches" alert={report.mismatched > 0} />
          <ValStat value={report.extra} label="Extra in target" />
        </div>
        <p className="muted" style={{ marginTop: 12 }}>
          {report.source_database} → {report.target_database}
          {report.tables_compared > 0 && (
            <>
              {" "}· rows over the {report.tables_compared.toLocaleString()} table
              {report.tables_compared === 1 ? "" : "s"} counted on both sides:{" "}
              {report.tables_estimated > 0 ? "≈ " : ""}{report.source_rows.toLocaleString()} source /{" "}
              {report.tables_estimated > 0 ? "≈ " : ""}{report.target_rows.toLocaleString()} target
            </>
          )}
        </p>
        {report.tables_estimated > 0 && (
          <p className="muted report-hero__disclaimer">
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
              <circle cx="12" cy="12" r="9" /><path d="M12 11v5" /><path d="M12 8h.01" />
            </svg>
            {report.tables_estimated.toLocaleString()} very large table
            {report.tables_estimated === 1 ? " was" : "s were"} compared using row-count
            estimates (planner statistics) rather than an exact scan, so those totals are
            approximate (≈).
          </p>
        )}
      </div>
    </section>
  );
}

function ValStat({ value, label, alert }: { value: number; label: string; alert?: boolean }) {
  return (
    <div className="stat">
      <div className="stat__text">
        <div className="stat__value" style={alert ? { color: "var(--err)" } : undefined}>{value.toLocaleString()}</div>
        <div className="stat__label">{label}</div>
      </div>
    </div>
  );
}

function MatchDonut({ value, tone }: { value: number; tone: Tone }) {
  const r = 52;
  const circ = 2 * Math.PI * r;
  const pct = Math.max(0, Math.min(100, value));
  const offset = circ * (1 - pct / 100);
  const color = tone === "ok" ? "var(--ok)" : tone === "warn" ? "var(--warn)" : "var(--err)";
  return (
    <div className="donut">
      <svg viewBox="0 0 120 120" width="120" height="120">
        <circle cx="60" cy="60" r={r} fill="none" stroke="var(--border)" strokeWidth="10" />
        <circle
          cx="60" cy="60" r={r} fill="none" stroke={color} strokeWidth="10" strokeLinecap="round"
          strokeDasharray={circ} strokeDashoffset={offset} transform="rotate(-90 60 60)"
          style={{ transition: "stroke-dashoffset .6s ease" }}
        />
      </svg>
      <div className="donut__center">
        <div className="donut__value">{value}%</div>
        <div className="donut__label">match</div>
      </div>
    </div>
  );
}

// --- AI repair agent (autonomous remediation) -----------------------------------------

// Mirror of backend/validation/agent.sql_fixable: what the agent can resolve with DDL.
// Extra (target-only) objects are never agent work — nothing to convert; they get
// the Remove from target action in the object list instead.
function sqlFixable(i: ValidationItem): boolean {
  if (i.status === "missing") return true;
  if (i.status === "extra") return false;
  return i.columns_missing.length > 0 || i.columns_extra.length > 0 || i.type_drift.length > 0 || !!i.fix_sql;
}

/** The repair-agent idea, moved here from Create Sync: one click hands every open
 *  inconsistency to an agent loop that generates the fix, applies it to Lakebase,
 *  and iterates on any Postgres error until the report is clean. When the run
 *  applied fixes, `onFixesApplied` re-validates the code objects automatically. */
function RepairAgentCard({ state, setState, fmEndpoint, llm, onFixesApplied }: {
  state: MigrationState;
  setState: React.Dispatch<React.SetStateAction<MigrationState>>;
  fmEndpoint?: string;
  llm?: string;
  onFixesApplied?: () => void;
}) {
  const report = state.validation!;
  const [attempts, setAttempts] = useState(3);
  const [repair, setRepair] = useState<RepairState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const pollRef = useRef(true);
  useEffect(() => () => { pollRef.current = false; }, []);

  // The agent converts source objects into the target (missing/mismatch); extra
  // objects aren't sent — removing them is a review-and-drop, not a conversion.
  const open = report.items.filter((i) => i.status !== "matched" && i.status !== "extra" && !i.remediated);
  const extras = report.items.filter((i) => i.status === "extra" && !i.remediated).length;
  const rowOnly = open.filter((i) => !sqlFixable(i)).length;
  const running = repair?.status === "running";

  function markRemediated(ids: Set<string>) {
    if (!ids.size) return;
    setState((s) => s.validation
      ? {
          ...s,
          validation: {
            ...s.validation,
            items: s.validation.items.map((i) => (ids.has(i.id) ? { ...i, remediated: true } : i)),
          },
        }
      : s);
  }

  async function resolve() {
    if (!state.lakebase || !open.length) return;
    setError(null);
    // Re-runs continue each item's attempt history instead of starting over.
    const prior = new Map((repair?.items ?? []).map((i) => [i.id, i.attempts]));
    // Re-arm the poll guard: StrictMode's simulated unmount runs the cleanup
    // that clears it, so it must be set on every start, not just at mount.
    pollRef.current = true;
    try {
      const { run_id } = await api.startValidationRepair({
        lakebase: state.lakebase,
        targets: open.map((item) => ({ item, prior_attempts: prior.get(item.id) ?? [] })),
        target_schema: state.targetSchema,
        endpoint: fmEndpoint || undefined,
        max_attempts: attempts,
      });
      const tick = async () => {
        if (!pollRef.current) return;
        try {
          const r = await api.validationRepairStatus(run_id);
          setRepair(r);
          if (r.status === "running") setTimeout(tick, 1200);
          else {
            markRemediated(new Set(r.items.filter((i) => i.status === "success").map((i) => i.id)));
            // Fixes landed in the target — verify them right away with a fast
            // objects-only re-validation (no row counts) so the report shows
            // what is genuinely still missing.
            if (r.fixed > 0) onFixesApplied?.();
          }
        } catch (e) {
          setError((e as Error).message);
        }
      };
      tick();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  if (!open.length && !repair) return null;

  return (
    <section className="card">
      <div className="card__head">
        <h3>✦ AI repair agent</h3>
        <span className="muted">
          {llm && (
            <span className="sbadge sbadge--ai" title="The Foundation Model serving endpoint behind the AI fixes — change it in Settings." style={{ marginRight: 10 }}>
              {llm}
            </span>
          )}
          {open.length === 0 ? "no open inconsistencies" : `${open.length} open inconsistenc${open.length === 1 ? "y" : "ies"}`}
        </span>
      </div>
      <p className="muted">
        Hands every open inconsistency to an autonomous agent: it generates the remediation SQL
        (translating source T-SQL where needed), applies it to Lakebase, and iterates on any
        Postgres error until the object is consistent — or it runs out of attempts.
        {rowOnly > 0 && (
          <> {rowOnly} row-count mismatch{rowOnly === 1 ? "" : "es"} can't be fixed by SQL — the agent
          flags {rowOnly === 1 ? "it" : "them"} for <strong>Re-copy table data</strong> below.</>
        )}
        {extras > 0 && (
          <> {extras} object{extras === 1 ? " exists" : "s exist"} only in the target — nothing to
          convert, so the agent skips {extras === 1 ? "it" : "them"}; review and use{" "}
          <strong>Remove from target</strong> in the object list below.</>
        )}
      </p>
      <AgentAttempts value={attempts} onChange={setAttempts} />
      <div className="actions">
        <button className="btn btn--primary" disabled={running || !open.length} onClick={resolve}>
          {running ? "Agent working…" : `✦ Resolve ${open.length.toLocaleString()} with the AI agent`}
        </button>
      </div>
      {error && <div className="banner banner--err">{error}</div>}
      {repair && <RepairAgentPanel repair={repair} />}
      {repair && !running && repair.fixed > 0 && (
        <div className="banner banner--ok">
          {repair.fixed.toLocaleString()} fix{repair.fixed === 1 ? "" : "es"} applied — re-checking
          the code objects against the target…
        </div>
      )}
    </section>
  );
}

function AgentAttempts({ value, onChange }: { value: number; onChange: (n: number) => void }) {
  return (
    <div className="agentattempts">
      <span>Max fix attempts per run</span>
      <select value={value} onChange={(e) => onChange(Number(e.target.value))}>
        {[1, 2, 3, 4, 5].map((n) => <option key={n} value={n}>{n}</option>)}
      </select>
      <span className="agentattempts__hint">re-running the agent continues where the last run stopped</span>
    </div>
  );
}

/** Live view of the agent run: overall progress plus, per inconsistency, the
 *  timeline of attempts — the agent's diagnosis, the fix it applied, and the
 *  outcome that feeds the next iteration. */
function RepairAgentPanel({ repair }: { repair: RepairState }) {
  const running = repair.status === "running";
  const total = repair.items.length;
  const pct = running ? Math.max(6, (repair.fixed / Math.max(1, total)) * 100) : 100;
  const overall = running ? "ai" : repair.status === "success" ? "ok" : "err";
  const overallLabel = running ? `working — attempt ${repair.attempt} of ${repair.max_attempts}` : repair.status;

  return (
    <div className="phasebox">
      <div className="prog__row">
        <span className="prog__name">✦ AI repair agent</span>
        <span className="prog__count">
          {repair.fixed.toLocaleString()} fixed · {repair.remaining.toLocaleString()} remaining
        </span>
        <span className={`sbadge sbadge--${overall}`}>{overallLabel}</span>
      </div>
      <div className="bar">
        <div className={`bar__fill bar__fill--${running ? "running" : repair.status === "success" ? "success" : "failed"}`} style={{ width: `${pct}%` }} />
      </div>
      {repair.error && <div className="banner banner--err">{repair.error}</div>}
      <div className="proglist" style={{ marginTop: 12 }}>
        {repair.items.map((it) => (
          <div key={it.id} className="agentitem">
            <div className="prog__row">
              <span className="prog__name">
                <span className="tag tag--kind">{it.kind}</span> {it.name}
              </span>
              <span className={`sbadge sbadge--${it.status === "success" ? "ok" : it.status === "failed" ? "err" : "ai"}`}>
                {it.status === "success" ? "fixed" : it.status === "failed" ? (it.gave_up ? "not SQL-fixable" : "failed") : it.status}
              </span>
            </div>
            <div className="agentsteps">
              <div className="agentstep">
                <div className="agentstep__head">Inconsistency</div>
                <div className="agentstep__err">{it.reason}</div>
              </div>
              {it.attempts.map((a) => (
                <div key={a.attempt} className="agentstep">
                  <div className="agentstep__head">
                    Attempt {a.attempt}
                    {a.status === "success" && <span className="agentstep__ok"> ✓ applied</span>}
                  </div>
                  {a.analysis && <div className="agentstep__analysis">✦ {a.analysis}</div>}
                  {a.sql && (
                    <details className="agentstep__sql">
                      <summary>SQL applied</summary>
                      <pre className="code__body">{a.sql}</pre>
                    </details>
                  )}
                  {a.status !== "success" && a.error && <div className="agentstep__err">{a.error}</div>}
                </div>
              ))}
              {it.status === "analyzing" && <div className="agentstep agentstep--live">Analyzing the inconsistency…</div>}
              {it.status === "applying" && <div className="agentstep agentstep--live">Applying the fix…</div>}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// --- Match list (grouped, filterable, expandable) ------------------------------------

function MatchList({ state, setState, fmEndpoint }: {
  state: MigrationState;
  setState: React.Dispatch<React.SetStateAction<MigrationState>>;
  fmEndpoint?: string;
}) {
  const report = state.validation!;
  const [active, setActive] = useState<Set<MatchStatus>>(new Set());
  const [query, setQuery] = useState("");
  const [openId, setOpenId] = useState<string | null>(null);
  const [openGroups, setOpenGroups] = useState<Record<string, boolean>>({});

  const toggleStatus = (s: MatchStatus) =>
    setActive((prev) => {
      const next = new Set(prev);
      next.has(s) ? next.delete(s) : next.add(s);
      return next;
    });

  const counts = useMemo(() => {
    const c: Record<MatchStatus, number> = { matched: 0, missing: 0, mismatch: 0, extra: 0 };
    report.items.forEach((i) => { c[i.status] += 1; });
    return c;
  }, [report.items]);

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return report.items.filter((i) =>
      (active.size === 0 || active.has(i.status)) &&
      (!q || i.source_name.toLowerCase().includes(q) || i.target_name.toLowerCase().includes(q)),
    );
  }, [report.items, active, query]);

  const groups = KIND_ORDER
    .map((kind) => ({ kind, items: shown.filter((i) => i.kind === kind) }))
    .filter((g) => g.items.length > 0);

  const issues = report.items.filter((i) => i.status !== "matched" && !i.remediated).length;

  return (
    <section className="card">
      <div className="card__head">
        <h3>Object-by-object match</h3>
        <span className="muted">
          {issues === 0 ? "no open inconsistencies" : `${issues} open inconsistenc${issues === 1 ? "y" : "ies"}`}
          {active.size > 0 && <> · <button className="link" onClick={() => setActive(new Set())}>clear filter</button></>}
        </span>
      </div>

      {issues === 0 && (
        <div className="success-state" style={{ marginBottom: 14 }}>
          <span className="success-state__icon">✓</span>
          <div>
            <strong>Source and Lakebase are consistent</strong>
            <p className="muted">Every compared object exists in the target with matching structure and row counts.</p>
          </div>
        </div>
      )}

      <div className="plantools">
        <div className="search-wrap">
          <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2">
            <circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" />
          </svg>
          <input className="search" placeholder="Search objects…" value={query} onChange={(e) => setQuery(e.target.value)} />
        </div>
        <div className="chipbar">
          {STATUS_ORDER.map((s) => (
            <button
              key={s}
              className={`chip ${active.has(s) ? "is-active" : ""}`}
              disabled={!counts[s]}
              onClick={() => toggleStatus(s)}
            >
              {STATUS_META[s].label} <span className="chip__n">{counts[s]}</span>
            </button>
          ))}
        </div>
      </div>

      <div className="plangroups">
        {groups.length === 0 && <p className="muted plan-empty">No objects match the current filter.</p>}
        {groups.map(({ kind, items }) => {
          const groupIssues = items.filter((i) => i.status !== "matched").length;
          const open = openGroups[kind] ?? groupIssues > 0;
          return (
            <div key={kind} className="plangroup">
              <button className="plangroup__head" onClick={() => setOpenGroups((g) => ({ ...g, [kind]: !open }))}>
                <Caret open={open} />
                <span className="plangroup__title">{KIND_LABEL[kind]}</span>
                <span className="plangroup__count">{items.length}</span>
                {groupIssues > 0 && <span className="sbadge sbadge--warn">{groupIssues} issue{groupIssues === 1 ? "" : "s"}</span>}
              </button>
              {open && (
                <div className="plangroup__items">
                  {items.map((item) => (
                    <MatchRow
                      key={item.id}
                      item={item}
                      open={openId === item.id}
                      onToggle={() => setOpenId(openId === item.id ? null : item.id)}
                      state={state}
                      setState={setState}
                      fmEndpoint={fmEndpoint}
                    />
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}

function Caret({ open }: { open: boolean }) {
  return (
    <svg className={`caret ${open ? "caret--open" : ""}`} viewBox="0 0 24 24" width="14" height="14"
         fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
      <path d="m9 6 6 6-6 6" />
    </svg>
  );
}

function MatchRow({ item, open, onToggle, state, setState, fmEndpoint }: {
  item: ValidationItem;
  open: boolean;
  onToggle: () => void;
  state: MigrationState;
  setState: React.Dispatch<React.SetStateAction<MigrationState>>;
  fmEndpoint?: string;
}) {
  const meta = STATUS_META[item.status];
  const rows = item.kind === "table" && item.source_rows != null;
  // Post-data rollups summarise many objects in one row — show the count so the
  // collapsed row already says how much was checked, not just pass/fail.
  const rollup = isRollup(item) ? item : null;
  return (
    <div className={`planrow ${open ? "is-open" : ""}`}>
      <button className="planrow__head" onClick={onToggle}>
        <Caret open={open} />
        <span className="tag tag--kind">{item.kind}</span>
        <span className="planrow__name">
          {item.source_name || item.target_name}
          {item.source_name && <span className="trow__target"> → {item.target_name}</span>}
        </span>
        {rollup && (
          <span className="trow__rows">
            {rollup.objects_present} / {rollup.objects_expected}
          </span>
        )}
        {rows && (
          <span className="trow__rows">
            {item.rows_approximate ? "≈ " : ""}
            {item.source_rows!.toLocaleString()} → {item.target_rows != null ? `${item.rows_approximate ? "≈ " : ""}${item.target_rows.toLocaleString()}` : "—"} rows
            {item.rows_approximate && <span className="trow__approx" title="Estimated for a very large table (planner statistics, not an exact count)">est.</span>}
          </span>
        )}
        {item.remediated && <span className="sbadge sbadge--ai">remediated</span>}
        {isNoteworthyMatch(item) && (
          <span className="sbadge sbadge--info" title={item.detail}>ⓘ why it's here</span>
        )}
        <span className={`sbadge sbadge--${meta.badge}`}>{meta.label}</span>
      </button>
      {open && (
        <div className="planrow__body">
          {item.detail && <div className="finding__detail">{item.detail}</div>}
          {item.recommendation && <div className="finding__rec">{item.recommendation}</div>}
          {/* The rollup's counts are only actionable if the user can see WHICH
              objects are missing — list them, worst first. */}
          {rollup && <ObjectList objects={rollup.objects!} />}
          {item.remediated && (
            <div className="banner banner--ok">A fix was applied — re-run the validation to verify it.</div>
          )}
          {item.status !== "matched" && (
            <FixPanel key={item.id} item={item} state={state} setState={setState} fmEndpoint={fmEndpoint} />
          )}
        </div>
      )}
    </div>
  );
}

// --- Remediation (AI or manual) -------------------------------------------------------

function FixPanel({ item, state, setState, fmEndpoint }: {
  item: ValidationItem;
  state: MigrationState;
  setState: React.Dispatch<React.SetStateAction<MigrationState>>;
  fmEndpoint?: string;
}) {
  const conn = state.connection;
  const [sql, setSql] = useState(item.fix_sql);
  const [analysis, setAnalysis] = useState<string | null>(null);
  const [aiBusy, setAiBusy] = useState(false);
  const [aiErr, setAiErr] = useState<string | null>(null);
  const [applyBusy, setApplyBusy] = useState(false);
  const [applied, setApplied] = useState<string | null>(null);
  const [applyErr, setApplyErr] = useState<string | null>(null);

  // Re-copy (row-count mismatches): reuses the data-migration engine for one table.
  const [copyRunId, setCopyRunId] = useState<string | null>(null);
  const [copyRun, setCopyRun] = useState<RunState | null>(null);
  const copyRef = useRef(true);
  useEffect(() => () => { copyRef.current = false; }, []);

  const tableInfo = item.kind === "table"
    ? state.report?.tables.find((t) => `${t.schema_name}.${t.table_name}` === item.source_name)
    : undefined;
  const rowsDiffer = item.source_rows != null && item.target_rows != null && item.source_rows !== item.target_rows;
  const canRecopy = item.status === "mismatch" && rowsDiffer && !!tableInfo;
  const copying = copyRun?.status === "running";
  // Extra objects aren't converted — the remediation is removal: the guarded
  // DROP is pre-filled deterministically, so there is no AI step to offer.
  const isDrop = item.status === "extra";

  function markRemediated() {
    setState((s) => s.validation
      ? {
          ...s,
          validation: {
            ...s.validation,
            items: s.validation.items.map((i) => (i.id === item.id ? { ...i, remediated: true } : i)),
          },
        }
      : s);
  }

  async function aiFix() {
    setAiBusy(true);
    setAiErr(null);
    try {
      const p = await api.proposeValidationFix({
        item,
        target_schema: state.targetSchema,
        endpoint: fmEndpoint || undefined,
      });
      if (!p.success) {
        setAiErr(p.error || "AI fix unavailable — choose a working Foundation Model endpoint in Settings.");
      } else {
        setAnalysis(p.analysis);
        if (p.sql.trim()) setSql(p.sql);
      }
    } catch (e) {
      setAiErr((e as Error).message);
    } finally {
      setAiBusy(false);
    }
  }

  async function apply() {
    if (!state.lakebase) return;
    // An empty password is fine — the backend resolves it from its session cache.
    setApplyBusy(true);
    setApplyErr(null);
    try {
      const res = await api.applyPlan({
        lakebase: state.lakebase,
        items: [{
          id: item.id, kind: item.kind, name: item.target_name, sql,
          original: item.source_definition, reasoning: analysis ?? "",
          notes: "Post-migration validation fix",
        }],
        stop_on_error: false,
      });
      const r = res.results[0];
      if (r?.status === "success") {
        markRemediated();
        setApplied(isDrop ? "Removed from the target." : "Applied to Lakebase.");
      } else {
        setApplyErr(r?.error || "Apply failed.");
      }
    } catch (e) {
      setApplyErr((e as Error).message);
    } finally {
      setApplyBusy(false);
    }
  }

  async function recopy() {
    if (!conn || !state.lakebase) {
      setApplyErr("No source connection configured — set it on the Connections & Target step.");
      return;
    }
    // Empty passwords are fine — /data/start resolves both sides from the session cache.
    setApplyErr(null);
    const { run_id } = await api.startData({
      source_type: conn.source_type, host: conn.host, database: conn.database,
      username: conn.username, password: conn.password, port: conn.port,
      secret_ref: conn.secret_ref, project_id: conn.project_id,
      lakebase: state.lakebase, target_schema: state.targetSchema,
      identifier_case: state.identifierCase,
      tables: [{
        schema_name: tableInfo!.schema_name, table_name: tableInfo!.table_name,
        total_rows: tableInfo!.row_count, columns: tableInfo!.columns,
      }],
      truncate_first: true, batch_size: state.dataOptions.batch_size,
    }).catch((e) => { setApplyErr((e as Error).message); return { run_id: null as string | null }; });
    if (!run_id) return;
    copyRef.current = true;
    setCopyRunId(run_id);
    const tick = async () => {
      if (!copyRef.current) return;
      try {
        const r = await api.dataStatus(run_id);
        setCopyRun(r);
        if (r.status === "running") setTimeout(tick, 1200);
        else if (r.status === "success") { markRemediated(); setApplied("Table re-copied."); }
        else setApplyErr(r.tables[0]?.error || r.error || "Re-copy failed.");
      } catch (e) {
        setApplyErr((e as Error).message);
      }
    };
    tick();
  }

  const hasStructDiff = item.kind === "table" &&
    (rowsDiffer || item.columns_missing.length > 0 || item.columns_extra.length > 0 || item.type_drift.length > 0);
  // A rollup's breakdown is already listed above the panel by MatchRow, so the
  // editor gets the full width instead of repeating it in a source pane.
  const hasSourcePane = (!!item.source_definition || hasStructDiff) && !isRollup(item);

  return (
    <div className="phasebox">
      <div className="prog__row">
        <span className="prog__name">Remediation</span>
        {isDrop && (
          <span className="pill-danger" title="Applying this SQL permanently drops the object from Lakebase — make sure it isn't needed.">
            <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden>
              <path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14M10 11v6M14 11v6" />
            </svg>
            destructive — drops the object
          </span>
        )}
        <span style={{ flex: 1 }} />
        {!isDrop && (
          <button className="btn btn--sm" disabled={aiBusy || copying} onClick={aiFix}>
            {aiBusy ? "Asking the AI agent…" : <><span className="ai-spark" aria-hidden>✦</span> Fix with AI</>}
          </button>
        )}
        {canRecopy && (
          <button className="btn btn--sm" disabled={copying || aiBusy} onClick={recopy}>
            {copying ? "Re-copying…" : "Re-copy table data"}
          </button>
        )}
      </div>

      {aiErr && <div className="banner banner--err">{aiErr}</div>}
      {analysis && <div className="agentstep__analysis" style={{ marginTop: 10 }}>✦ {analysis}</div>}

      {copyRunId && copyRun && (
        <div className="prog" style={{ marginTop: 12 }}>
          <div className="prog__row">
            <span className="prog__name">{copyRun.tables[0]?.name}</span>
            <span className="prog__count">
              {copyRun.tables[0]?.rows_copied.toLocaleString()}
              {copyRun.tables[0]?.total_rows ? ` / ${copyRun.tables[0].total_rows.toLocaleString()}` : ""} rows
            </span>
            <span className={`sbadge sbadge--${copyRun.status === "success" ? "ok" : copyRun.status === "running" ? "ai" : "err"}`}>
              {copyRun.status}
            </span>
          </div>
          <div className="bar">
            <div
              className={`bar__fill bar__fill--${copyRun.status === "success" ? "success" : copyRun.status === "running" ? "running" : "failed"}`}
              style={{ width: copyRun.tables[0]?.total_rows ? `${Math.min(100, (copyRun.tables[0].rows_copied / copyRun.tables[0].total_rows) * 100)}%` : "100%" }}
            />
          </div>
        </div>
      )}

      {/* Side-by-side compare: what the source has (left) vs the fix to apply (right). */}
      <div className={`fixgrid ${hasSourcePane ? "" : "fixgrid--single"}`}>
        {item.source_definition ? (
          <FixPane title={`Source · ${item.source_name} (T-SQL)`} copyText={item.source_definition}>
            <pre className="code__body fixpane__code">{item.source_definition}</pre>
          </FixPane>
        ) : hasStructDiff ? (
          <FixPane title="Source ⇄ target differences">
            <TableDiff item={item} />
          </FixPane>
        ) : null}

        <FixPane title={isDrop ? "Removal SQL · PostgreSQL (editable)" : "Fix to apply · PostgreSQL (editable)"}>
          <textarea
            className="sqlbox fixpane__editor"
            value={sql}
            placeholder={item.status === "mismatch" && rowsDiffer && !item.fix_sql
              ? "Row differences can't be fixed by DDL — use Re-copy table data, or paste SQL here."
              : "Postgres SQL to apply to Lakebase — generate it with AI or write it manually."}
            onChange={(e) => setSql(e.target.value)}
          />
        </FixPane>
      </div>

      <div className="actions">
        <button className={`btn ${isDrop ? "btn--danger" : "btn--primary"}`} disabled={!sql.trim() || applyBusy || copying} onClick={apply}>
          {applyBusy ? (isDrop ? "Removing…" : "Applying…") : isDrop ? "Remove from target" : "Apply to Lakebase"}
        </button>
      </div>

      {applied && <div className="banner banner--ok">{applied} Re-run the validation to verify.</div>}
      {applyErr && <div className="banner banner--err">{applyErr}</div>}
    </div>
  );
}

/** Titled code-style pane used by the side-by-side remediation compare. */
function FixPane({ title, copyText, children }: { title: string; copyText?: string; children: React.ReactNode }) {
  const [copied, setCopied] = useState(false);
  function copy() {
    navigator.clipboard.writeText(copyText!).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  }
  return (
    <div className="fixpane">
      <div className="code fixpane__frame">
        <div className="code__bar">
          <span className="code__lang">{title}</span>
          {copyText && (
            <div className="code__actions">
              <button className="btn btn--sm" onClick={copy}>{copied ? "Copied" : "Copy"}</button>
            </div>
          )}
        </div>
        {children}
      </div>
    </div>
  );
}

/** Structured source-vs-target diff for a mismatched table (rows + columns). */
/** The objects behind a constraint/index/FK rollup, problems first — a "3 of 5
 *  present" count is only useful next to the names of the missing two. Matched
 *  objects are listed too (collapsed to the tail) so the row also answers
 *  "what DID migrate?". */
function ObjectList({ objects }: { objects: ObjectDiff[] }) {
  const SIGN: Record<MatchStatus, { sign: string; cls: string }> = {
    missing: { sign: "−", cls: "err" },
    mismatch: { sign: "≠", cls: "warn" },
    extra: { sign: "+", cls: "info" },
    matched: { sign: "✓", cls: "ok" },
  };
  const ordered = [...objects].sort(
    (a, b) => STATUS_ORDER.indexOf(a.status) - STATUS_ORDER.indexOf(b.status),
  );
  return (
    <div className="coldiff">
      {ordered.map((o, i) => (
        <div key={i} className="coldiff__row">
          <span className={`coldiff__sign coldiff__sign--${SIGN[o.status].cls}`}>
            {SIGN[o.status].sign}
          </span>
          <span className="coldiff__name">{o.name}</span>
          <span className="coldiff__note">
            {o.detail}
            {/* Check predicates and defaults are compared by existence, not text
                (T-SQL and Postgres spell the same predicate differently), so show
                the source expression for the user to eyeball. */}
            {o.source_definition && (
              <>
                {o.detail ? " · " : ""}
                <code>{o.source_definition}</code>
              </>
            )}
          </span>
        </div>
      ))}
    </div>
  );
}

function TableDiff({ item }: { item: ValidationItem }) {
  const rows: { sign: string; cls: string; name: string; note: string }[] = [];
  if (item.source_rows != null && item.target_rows != null && item.source_rows !== item.target_rows) {
    rows.push({
      sign: "≠", cls: "warn", name: "row count",
      note: `source ${item.source_rows.toLocaleString()} · target ${item.target_rows.toLocaleString()} · Δ ${Math.abs(item.source_rows - item.target_rows).toLocaleString()}`,
    });
  }
  item.columns_missing.forEach((c) => rows.push({ sign: "−", cls: "err", name: c, note: "column missing in target" }));
  item.columns_extra.forEach((c) => rows.push({ sign: "+", cls: "info", name: c, note: "column only in target" }));
  item.type_drift.forEach((d) => {
    const [name, ...rest] = d.split(":");
    rows.push({ sign: "≠", cls: "warn", name: name.trim(), note: rest.join(":").trim() });
  });
  return (
    <div className="coldiff">
      {rows.map((r, i) => (
        <div key={i} className="coldiff__row">
          <span className={`coldiff__sign coldiff__sign--${r.cls}`}>{r.sign}</span>
          <span className="coldiff__name">{r.name}</span>
          <span className="coldiff__note">{r.note}</span>
        </div>
      ))}
    </div>
  );
}
