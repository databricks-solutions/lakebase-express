import { useCallback, useEffect, useState } from "react";
import { api, type MigrationReport as Report } from "../api";
import { ProgressBar } from "../components/Progress";
import ReportExport from "../components/ReportExport";

interface Props {
  projectId: string;
  /** Flushes the debounced autosave — the report is built from the *saved* project. */
  onSave: () => Promise<void>;
}

/** Hands over the audit record: opens the printable report, or downloads the file. */
export default function MigrationReport({ projectId, onSave }: Props) {
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);

  const load = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      await onSave().catch(() => {});
      setReport(await api.migrationReport(projectId));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }, [projectId, onSave]);

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  const covers = report ? coverage(report) : "";

  return (
    <div className="stack">
      <section className="card">
        <h2>Close the audit cycle</h2>
        <p className="muted">
          One report covering the whole migration — what the source looked like, what the plan was
          going to create, what the run actually copied, how source and target compared afterwards,
          and where the two still disagree. It is derived from what each phase recorded, so no
          database is queried to produce it, and it carries no password.
        </p>
        <ProgressBar active={busy} label="Building the report from this migration…" doneLabel="Ready" />
        {error && <div className="banner banner--err">{error}</div>}
        {!busy && covers && <p className="muted">Covers {covers}.</p>}
      </section>

      {report && (
        <>
          <section className="card">
            <div className="savebar">
              <div>
                <h3 style={{ margin: 0 }}>Hand it over</h3>
                <p className="muted">
                  Opens as a printable page — use your browser's <strong>Print → Save as PDF</strong>{" "}
                  (there is a button on the page too) for the PDF, or download the HTML to commit or
                  attach. Both are one self-contained file with nothing external to load.
                </p>
              </div>
              <div className="savebar__actions">
                <ReportExport
                  projectId={projectId}
                  projectName={report.provenance.project_name}
                  onSave={onSave}
                  onError={setError}
                />
              </div>
            </div>
          </section>

          <section className="card">
            <h3>What it will say</h3>
            <div className="statgrid statgrid--4">
              <Score value={report.headline.readiness_score} label="Readiness" suffix="/100" />
              <Score value={report.headline.match_score} label="Validation match" suffix="%" />
              <Score value={report.headline.parity_score} label="Query parity" suffix="%" />
              <Score value={report.headline.rows_copied} label="Rows copied" />
            </div>
            <ul className="assumptions" style={{ marginTop: 14 }}>
              {phases(report).map((line, i) => (
                <li key={i}>{line}</li>
              ))}
            </ul>
          </section>

          {report.provenance.completeness.length > 0 && (
            <section className="card">
              <h3>Before you send it</h3>
              <p className="muted">
                The report states each of these itself, at the top, so a reader cannot mistake a
                phase that never ran for one that passed.
              </p>
              <ul className="assumptions">
                {report.provenance.completeness.map((c, i) => (
                  <li key={i}>{c}</li>
                ))}
              </ul>
            </section>
          )}
        </>
      )}
    </div>
  );
}

/** A score whose phase never ran reads "Not run" — never 0, which is a real result. */
function Score({ value, label, suffix = "" }: { value: number | null; label: string; suffix?: string }) {
  return (
    <div className="stat">
      <div className="stat__text">
        <div className="stat__value" style={value === null ? { color: "var(--muted-2)", fontSize: 15 } : undefined}>
          {value === null ? "Not run" : `${value.toLocaleString()}${suffix}`}
        </div>
        <div className="stat__label">{label}</div>
      </div>
    </div>
  );
}

function count(n: number, one: string, many: string): string {
  return `${n.toLocaleString()} ${n === 1 ? one : many}`;
}

/** One line per phase, saying what the report carries from it or that it is absent. */
function phases(report: Report): string[] {
  const out: string[] = [];
  const { assessment, plan, result, validation, parity } = report;
  out.push(
    assessment
      ? `Assessment — ${count(assessment.table_count, "table", "tables")}, ` +
        `${count(assessment.total_rows, "row", "rows")}, ` +
        `${count(assessment.programmable_object_count, "code object", "code objects")}, and ` +
        `${count(assessment.findings_total, "compatibility finding", "compatibility findings")}` +
        (assessment.ai ? ", plus the AI analysis" : "")
      : "Assessment — the source was never scanned.",
  );
  const origins = [
    plan?.translated ? `${plan.translated} AI-translated` : "",
    plan?.user_edited ? `${plan.user_edited} edited by hand` : "",
    plan?.not_translated ? `${plan.not_translated} not translated` : "",
  ].filter(Boolean);
  out.push(
    plan
      ? `Plan — ${count(plan.total, "object", "objects")} (${plan.pre_data} before the data load, ` +
        `${plan.post_data} after)` +
        (plan.code_objects_total
          ? `, including ${count(plan.code_objects_total, "code object", "code objects")}: ${origins.join(", ")}`
          : "")
      : "Plan — no plan was built.",
  );
  out.push(
    result?.runs_total
      ? `Result — ${count(result.rows_copied, "row", "rows")} copied across ` +
        `${count(result.tables_loaded, "table", "tables")}, from ` +
        `${count(result.runs_total, "recorded run", "recorded runs")}`
      : "Result — no data load is recorded against this project.",
  );
  out.push(
    validation
      ? `Validation — ${validation.match_score}% matched` +
        (validation.outstanding_total
          ? `, with ${count(validation.outstanding_total, "object", "objects")} listed as outstanding`
          : ", with nothing outstanding")
      : "Validation — has not been run.",
  );
  out.push(
    parity
      ? `Query parity — ${parity.parity_score}% of ${count(parity.total, "query pair", "query pairs")} agreed` +
        (parity.speedup ? `; ${parity.speedup}× the source's total execution time` : "")
      : "Query parity — has not been run.",
  );
  return out;
}

function coverage(report: Report): string {
  const parts: string[] = [];
  if (report.assessment) parts.push(count(report.assessment.findings_total, "finding", "findings"));
  if (report.plan) parts.push(count(report.plan.total, "planned object", "planned objects"));
  if (report.result?.runs_total) parts.push(count(report.result.runs_total, "run", "runs"));
  if (report.validation?.outstanding_total) {
    parts.push(count(report.validation.outstanding_total, "outstanding object", "outstanding objects"));
  }
  if (report.parity) parts.push(count(report.parity.total, "query pair", "query pairs"));
  return parts.join(" · ");
}
