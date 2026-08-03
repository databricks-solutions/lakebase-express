import { useState } from "react";
import { api, type AIAssessment, type AIRisk, type AssessmentReport, type Severity } from "../api";
import type { MigrationState } from "../App";
import { ProgressBar } from "../components/Progress";
import Sizing from "./Sizing";

interface Props {
  state: MigrationState;
  setState: React.Dispatch<React.SetStateAction<MigrationState>>;
  goConnection: () => void;
  fmEndpoint?: string;
}

const SEVERITY_ORDER: Severity[] = ["high", "medium", "low", "info"];

export default function AssessmentModule({ state, setState, goConnection, fmEndpoint }: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [assessOpen, setAssessOpen] = useState(true);
  const [sizingOpen, setSizingOpen] = useState(false);
  const report = state.report;
  const hasConn = !!state.connection?.host;

  async function scan() {
    setBusy(true);
    setError(null);
    try {
      const r = await api.scan(state.connection!, fmEndpoint || undefined);
      setState((s) => ({ ...s, report: r }));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack">
      {!hasConn && (
        <div className="card">
          <h2>Source not configured</h2>
          <p className="muted">Set up and test the source connection before scanning.</p>
          <div className="actions"><button className="btn btn--primary" onClick={goConnection}>Go to Connection</button></div>
        </div>
      )}

      {/* Panel 1 — Assessment (open by default) */}
      <div className="acc">
        <button className="acc__head" aria-expanded={assessOpen} onClick={() => setAssessOpen((o) => !o)}>
          <div className="acc__head-text">
            <h2>Compatibility assessment</h2>
            <p className="muted">Scan the source schema, data types, and T-SQL objects, and report Lakebase compatibility.</p>
          </div>
          <Chevron open={assessOpen} />
        </button>

        {assessOpen && (
          <div className="acc__body">
            <section className="card">
              <div className="card__head">
                <h3>Source scan</h3>
                {report && <span className="muted">{report.database}</span>}
              </div>
              <p className="muted">
                A deterministic scan inventories user schemas, tables, data types, and code objects
                (system objects excluded) and applies the T-SQL → Postgres rule checks. An{" "}
                <strong>AI migration architect</strong> then reasons over the schema and code to surface
                deeper semantic, behavioral, and operational risks.
              </p>
              <div className="actions">
                <button className="btn btn--primary" disabled={busy || !hasConn} onClick={scan}>
                  {busy ? "Scanning & analyzing with AI…" : report ? "Re-run assessment" : "Run assessment"}
                </button>
              </div>
              <ProgressBar
                active={busy}
                label="Scanning source schema & analyzing with AI…"
                doneLabel="Assessment complete"
              />
              {error && <div className="banner banner--err">{error}</div>}
            </section>

            {report && <ReportView report={report} />}
            {report && <AIAnalysis ai={report.ai_assessment} busy={busy} onRetry={scan} />}
          </div>
        )}
      </div>

      {/* Panel 2 — Sizing & Cost (collapsed by default) */}
      <div className="acc">
        <button className="acc__head" aria-expanded={sizingOpen} onClick={() => setSizingOpen((o) => !o)}>
          <div className="acc__head-text">
            <h2>Sizing &amp; Cost</h2>
            <p className="muted">Map the source capacity to Lakebase capacity units and estimate monthly cost.</p>
          </div>
          <Chevron open={sizingOpen} />
        </button>

        {sizingOpen && (
          <div className="acc__body">
            <Sizing state={state} />
          </div>
        )}
      </div>
    </div>
  );
}

function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      className={`acc__chevron ${open ? "acc__chevron--open" : ""}`}
      viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="2"
      aria-hidden
    >
      <path d="m6 9 6 6 6-6" />
    </svg>
  );
}

type Tone = "ok" | "warn" | "err";

function toneFor(score: number): Tone {
  return score >= 80 ? "ok" : score >= 50 ? "warn" : "err";
}

const VERDICT: Record<Tone, { title: string; desc: string }> = {
  ok: { title: "Ready to migrate", desc: "No blocking issues — schema and code can move to Lakebase with minimal changes." },
  warn: { title: "Migrate with review", desc: "Some constructs need manual review before they're applied to Lakebase." },
  err: { title: "Significant rework", desc: "Several high-severity constructs require redesign before migrating." },
};

const STAT_ICONS = {
  tables: (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2">
      <rect x="3" y="4" width="18" height="16" rx="2" /><path d="M3 9h18M3 14h18M9 4v16" />
    </svg>
  ),
  rows: (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M4 6h16M4 10h16M4 14h16M4 18h10" />
    </svg>
  ),
  code: (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="m8 8-4 4 4 4M16 8l4 4-4 4M13 6l-2 12" />
    </svg>
  ),
};

function ReportView({ report }: { report: AssessmentReport }) {
  const tone = toneFor(report.readiness_score);
  const verdict = VERDICT[tone];
  const total = report.findings.length;

  // Click a severity to filter the findings list (multi-select; empty = show all).
  const [active, setActive] = useState<Set<Severity>>(new Set());
  const toggleSev = (s: Severity) =>
    setActive((prev) => {
      const next = new Set(prev);
      next.has(s) ? next.delete(s) : next.add(s);
      return next;
    });
  const shown = active.size ? report.findings.filter((f) => active.has(f.severity)) : report.findings;

  return (
    <>
      <section className="card report-hero">
        <ScoreDonut value={report.readiness_score} tone={tone} />
        <div className="report-hero__main">
          <span className={`verdict-chip verdict-chip--${tone}`}>{verdict.title}</span>
          <p className="muted report-hero__desc">{verdict.desc}</p>
          <div className="statgrid">
            <Stat icon={STAT_ICONS.tables} label="Tables" value={report.table_count} />
            <Stat icon={STAT_ICONS.rows} label="Rows" value={report.total_rows.toLocaleString()} />
            <Stat icon={STAT_ICONS.code} label="Code objects" value={report.programmable_object_count} sub="procedures · views · triggers" />
          </div>
        </div>
      </section>

      <section className="card">
        <div className="card__head">
          <h3>Compatibility findings</h3>
          <span className="muted">
            {total === 0
              ? "None"
              : active.size
                ? `${shown.length} of ${total} shown`
                : `${total} finding${total === 1 ? "" : "s"}`}
            {active.size > 0 && (
              <>
                {" · "}
                <button className="link" onClick={() => setActive(new Set())}>clear filter</button>
              </>
            )}
          </span>
        </div>

        {total === 0 ? (
          <div className="success-state">
            <span className="success-state__icon">✓</span>
            <div>
              <strong>No issues detected</strong>
              <p className="muted">Every scanned object maps cleanly to Postgres — this migration is fully automatable.</p>
            </div>
          </div>
        ) : (
          <>
            <SeverityBar counts={report.severity_counts} active={active} onToggle={toggleSev} />
            <div className="findings">
              {shown.map((f, i) => (
                <div key={i} className={`finding finding--${f.severity}`}>
                  <span className={`tag tag--${f.severity}`}>{f.severity}</span>
                  <div className="finding__body">
                    <div className="finding__title">
                      <strong>{f.title}</strong> <span className="muted">· {f.object_name}</span>
                    </div>
                    <div className="finding__detail">{f.detail}</div>
                    <div className="finding__rec">{f.recommendation}</div>
                  </div>
                </div>
              ))}
            </div>
          </>
        )}
      </section>
    </>
  );
}

function complexityTone(c: string): Tone {
  const v = (c || "").toLowerCase();
  return v === "high" ? "err" : v === "low" ? "ok" : "warn";
}

function riskSeverity(s: string): Severity {
  const v = (s || "").toLowerCase();
  return v === "high" ? "high" : v === "low" ? "low" : v === "info" ? "info" : "medium";
}

function AIAnalysis({ ai, busy, onRetry }: { ai?: AIAssessment | null; busy: boolean; onRetry: () => void }) {
  // While the scan+AI call is in flight and we have no prior result, show a thinking state.
  if (busy && !ai) {
    return (
      <section className="card ai-card">
        <div className="ai-card__head">
          <h3><span className="ai-spark" aria-hidden>✦</span> AI migration analysis</h3>
        </div>
        <p className="muted ai-thinking">The migration architect is reasoning over the schema and code…</p>
      </section>
    );
  }
  if (!ai) return null;

  return (
    <section className="card ai-card">
      <div className="ai-card__head">
        <h3><span className="ai-spark" aria-hidden>✦</span> AI migration analysis</h3>
        {ai.endpoint && <span className="muted">{ai.endpoint}</span>}
      </div>

      {!ai.success ? (
        <div className="banner banner--err">
          AI analysis unavailable{ai.error ? `: ${ai.error}` : ""}. The rule-based assessment above is
          unaffected — choose a working Foundation Model endpoint in Settings and re-run.{" "}
          <button className="link" onClick={onRetry}>Re-run</button>
        </div>
      ) : (
        <>
          <div className="ai-verdict">
            <span className={`complexity complexity--${complexityTone(ai.complexity)}`}>
              {ai.complexity || "Medium"} complexity
            </span>
          </div>

          {ai.summary && <p className="ai-summary">{ai.summary}</p>}
          {ai.complexity_rationale && <p className="muted">{ai.complexity_rationale}</p>}

          {ai.risks.length > 0 && (
            <>
              <h4 className="ai-subhead">Key migration risks <span className="muted">({ai.risks.length})</span></h4>
              <div className="findings">
                {ai.risks.map((r, i) => <AIRiskCard key={i} risk={r} />)}
              </div>
            </>
          )}

          {ai.recommendations.length > 0 && (
            <>
              <h4 className="ai-subhead">Recommendations</h4>
              <ul className="ai-recs">
                {ai.recommendations.map((r, i) => <li key={i}>{r}</li>)}
              </ul>
            </>
          )}

          <p className="ai-note">
            <span aria-hidden>✦</span> AI-generated analysis — review before acting. The readiness score and
            findings above are deterministic.
          </p>
        </>
      )}
    </section>
  );
}

function AIRiskCard({ risk }: { risk: AIRisk }) {
  const sev = riskSeverity(risk.severity);
  return (
    <div className={`finding finding--${sev}`}>
      <span className={`tag tag--${sev}`}>{sev}</span>
      <div className="finding__body">
        <div className="finding__title">
          <strong>{risk.title}</strong>
          {risk.category && <span className="airisk__cat">{risk.category}</span>}
        </div>
        {risk.affected_objects && <div className="finding__detail">Affects: {risk.affected_objects}</div>}
        {risk.rationale && <div className="finding__detail">{risk.rationale}</div>}
        {risk.recommendation && <div className="finding__rec">{risk.recommendation}</div>}
      </div>
    </div>
  );
}

function Stat({ icon, label, value, sub }: { icon: JSX.Element; label: string; value: string | number; sub?: string }) {
  return (
    <div className="stat">
      <span className="stat__icon">{icon}</span>
      <div className="stat__text">
        <div className="stat__value">{value}</div>
        <div className="stat__label">{label}</div>
        {sub && <div className="stat__sub">{sub}</div>}
      </div>
    </div>
  );
}

function SeverityBar({
  counts,
  active,
  onToggle,
}: {
  counts: Record<Severity, number>;
  active: Set<Severity>;
  onToggle: (s: Severity) => void;
}) {
  const present = SEVERITY_ORDER.filter((s) => (counts[s] ?? 0) > 0);
  return (
    <div className="sevbar">
      <div className="sevbar__track">
        {present.map((s) => {
          const dim = active.size > 0 && !active.has(s);
          return (
            <button
              key={s}
              type="button"
              className={`sevbar__seg sevbar__seg--${s} ${active.has(s) ? "is-active" : ""} ${dim ? "is-dim" : ""}`}
              style={{ flex: counts[s] }}
              title={`${counts[s]} ${s} — click to filter`}
              onClick={() => onToggle(s)}
            />
          );
        })}
      </div>
      <div className="sevbar__legend">
        {SEVERITY_ORDER.map((s) => {
          const n = counts[s] ?? 0;
          return (
            <button
              key={s}
              type="button"
              className={`sevbar__legend-item ${active.has(s) ? "is-active" : ""}`}
              disabled={!n}
              aria-pressed={active.has(s)}
              onClick={() => onToggle(s)}
            >
              <span className={`sevdot sevdot--${s}`} />
              {n} {s}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function ScoreDonut({ value, tone }: { value: number; tone: Tone }) {
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
        <div className="donut__value">{value}</div>
        <div className="donut__label">readiness</div>
      </div>
    </div>
  );
}
