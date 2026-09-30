"""Renders a MigrationReport as one self-contained HTML page that prints to PDF.

No external stylesheet, font, or script: the file can be emailed, committed, or
opened offline years later. The PDF is the browser's own print output — the
alternative (WeasyPrint, wkhtmltopdf) needs system libraries the Databricks Apps
container does not carry, and would render this same HTML worse.
"""
from __future__ import annotations

from datetime import datetime
from html import escape

from backend.report.models import (
    SCOPE_ASSESSMENT,
    LoadRun,
    MigrationReport,
    ParitySection,
    ValidationSection,
)

_KIND_LABEL = {
    "sync_run": "In-app load",
    "async_run": "Databricks job run",
    "async_job": "Databricks job provisioned",
}

_PROVENANCE_LABEL = {
    "ai": "AI-translated",
    "user-edited": "Edited by hand",
    "not-translated": "Not translated",
}

_STATUS_TONE = {
    "success": "ok", "matched": "ok", "match": "ok", "created": "ok", "scheduled": "ok",
    "partial": "warn", "mismatch": "warn", "mismatched": "warn", "extra": "warn",
    "skipped": "warn", "running": "warn", "submitted": "warn",
    "failed": "err", "missing": "err", "error": "err",
}

_CSS = """
:root {
  --ink: #11171c; --ink-2: #1b3139; --muted: #5f6b75; --muted-2: #8a96a0;
  --border: #e0e3e7; --bg-subtle: #f6f7f9; --primary: #2272b4; --accent: #ff3621;
  --ok: #1a8754; --ok-weak: #e7f4ec; --warn: #b35900; --warn-weak: #fbf0e0;
  --err: #c82d2d; --err-weak: #fbe9e9;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg-subtle); color: var(--ink);
  font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  font-size: 13px; line-height: 1.5; -webkit-print-color-adjust: exact; print-color-adjust: exact;
}
.sheet { max-width: 900px; margin: 0 auto; padding: 28px 32px 56px; background: #fff; }
h1 { font-size: 23px; margin: 0 0 4px; color: var(--ink-2); letter-spacing: -0.01em; }
h2 {
  font-size: 16px; color: var(--ink-2); margin: 30px 0 10px;
  padding-bottom: 6px; border-bottom: 2px solid var(--ink-2);
}
h3 { font-size: 13px; color: var(--ink-2); margin: 18px 0 6px; }
p { margin: 0 0 10px; }
.muted { color: var(--muted); }
.small { font-size: 11.5px; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px;
  background: #eef0f2; padding: 1px 4px; border-radius: 3px; }

.cover { border-bottom: 3px solid var(--accent); padding-bottom: 14px; margin-bottom: 4px; }
.cover__brand { font-size: 11px; text-transform: uppercase; letter-spacing: .09em;
  color: var(--muted-2); font-weight: 700; margin-bottom: 8px; }
.cover__sub { color: var(--muted); margin: 0; }

.flow { display: flex; flex-wrap: wrap; gap: 10px; margin: 16px 0 4px; }
.flow__box { flex: 1 1 240px; border: 1px solid var(--border); border-radius: 6px; padding: 10px 12px; }
.flow__label { font-size: 10.5px; text-transform: uppercase; letter-spacing: .07em;
  color: var(--muted-2); font-weight: 700; }

.scores { display: flex; flex-wrap: wrap; gap: 10px; margin: 14px 0; }
.score { flex: 1 1 120px; border: 1px solid var(--border); border-radius: 6px; padding: 10px 12px; }
.score__n { font-size: 21px; font-weight: 700; color: var(--ink-2); line-height: 1.2; }
.score__n--na { color: var(--muted-2); font-size: 15px; }
.score__l { font-size: 10.5px; text-transform: uppercase; letter-spacing: .06em;
  color: var(--muted-2); font-weight: 700; }

table { width: 100%; border-collapse: collapse; margin: 8px 0 14px; font-size: 12px; }
thead { display: table-header-group; }
th { text-align: left; font-size: 10.5px; text-transform: uppercase; letter-spacing: .05em;
  color: var(--muted); border-bottom: 1px solid var(--border); padding: 6px 8px 5px; font-weight: 700; }
td { padding: 6px 8px; border-bottom: 1px solid var(--border); vertical-align: top; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
tbody tr:nth-child(even) { background: #fafbfc; }

.pill { display: inline-block; font-size: 10.5px; font-weight: 700; padding: 1px 7px;
  border-radius: 9px; background: var(--bg-subtle); color: var(--muted); white-space: nowrap; }
.pill--ok { background: var(--ok-weak); color: var(--ok); }
.pill--warn { background: var(--warn-weak); color: var(--warn); }
.pill--err { background: var(--err-weak); color: var(--err); }

.note { border-left: 3px solid var(--primary); background: #f4f8fc; padding: 9px 12px;
  margin: 10px 0 14px; border-radius: 0 4px 4px 0; }
.note--warn { border-left-color: var(--warn); background: var(--warn-weak); }
.note ul { margin: 6px 0 0; padding-left: 18px; }
.note li { margin-bottom: 4px; }

.item { border: 1px solid var(--border); border-left: 3px solid var(--muted-2);
  border-radius: 0 5px 5px 0; padding: 9px 12px; margin-bottom: 9px; }
.item--high, .item--err { border-left-color: var(--err); }
.item--medium, .item--warn { border-left-color: var(--warn); }
.item--low, .item--info { border-left-color: var(--primary); }
.item__head { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
.item__title { font-weight: 700; color: var(--ink-2); }
.item__body { margin: 5px 0 0; }
.item dl { display: grid; grid-template-columns: max-content 1fr; gap: 2px 10px; margin: 6px 0 0;
  font-size: 11.5px; }
.item dt { color: var(--muted); }
.item dd { margin: 0; }

footer { margin-top: 34px; padding-top: 12px; border-top: 1px solid var(--border);
  color: var(--muted); font-size: 11.5px; }

.toolbar { position: sticky; top: 0; z-index: 5; display: flex; justify-content: flex-end;
  gap: 8px; padding: 8px 0 0; }
.toolbar button { font: inherit; font-size: 12px; font-weight: 600; cursor: pointer;
  border: 1px solid var(--primary); background: var(--primary); color: #fff;
  padding: 6px 13px; border-radius: 5px; }
.toolbar button:hover { background: #195487; }

@page { size: A4; margin: 13mm 11mm 15mm; }
@media print {
  body { background: #fff; font-size: 10.5pt; }
  .sheet { max-width: none; padding: 0; }
  .toolbar { display: none; }
  h2 { break-after: avoid; margin-top: 22px; }
  h3 { break-after: avoid; }
  tr, .item, .score, .flow__box, .note { break-inside: avoid; }
  /* Per paragraph, not the whole block: keeping the footer together would push it onto
     a page of its own on a short report, while a split mid-sentence reads as a fault. */
  footer { margin-top: 18px; }
  footer p { break-inside: avoid; }
  a[href^="http"]::after { content: " (" attr(href) ")"; font-size: 8pt; color: #5f6b75;
    word-break: break-all; }
}
"""


def _e(text: object) -> str:
    return escape(str(text if text is not None else ""), quote=True)


def _n(value: int | float | None) -> str:
    """Thousands-separated, or an em dash when the number was never measured."""
    if value is None:
        return "—"
    return f"{value:,}"


def _tone(status: str) -> str:
    return _STATUS_TONE.get((status or "").lower(), "")


def _pill(text: str, tone: str = "") -> str:
    suffix = f" pill--{tone}" if tone else ""
    return f'<span class="pill{suffix}">{_e(text)}</span>'


def _status_pill(status: str) -> str:
    return _pill(status or "unknown", _tone(status))


def _date(iso: str) -> str:
    """"29 Sep 2026 14:32 UTC" — the ISO stamp stays in the footer for the audit trail."""
    if not iso:
        return ""
    try:
        stamp = datetime.fromisoformat(iso)
    except ValueError:
        return iso[:19]
    return stamp.strftime("%d %b %Y %H:%M") + (" UTC" if stamp.tzinfo else "")


def _ms(value: int) -> str:
    if value and value >= 1000:
        return f"{value / 1000:.1f}s"
    return f"{value} ms"


def _count(n: int, one: str, many: str) -> str:
    return f"{n:,} {one if n == 1 else many}"


def _more(shown: int, total: int, what: str, where: str) -> str:
    """Says what a capped list left out, and where the whole of it lives.

    Not "see the JSON export": the caps are applied when the report is built, so the
    JSON carries the same ones. Only the project itself holds everything.
    """
    if total <= shown:
        return ""
    return (
        f'<p class="muted small">…and {total - shown} more {_e(what)}, not listed here. '
        f"This report summarises; the project's {_e(where)} holds all of them.</p>"
    )


def render_report(report: MigrationReport) -> str:
    """The full HTML document for a report."""
    out: list[str] = []
    w = out.append
    assessment_only = report.scope == SCOPE_ASSESSMENT
    kind = "Assessment report" if assessment_only else "Migration report"

    w("<!doctype html>")
    w('<html lang="en"><head>')
    w('<meta charset="utf-8">')
    w('<meta name="viewport" content="width=device-width, initial-scale=1">')
    w(f"<title>{_e(kind)} — {_e(report.provenance.project_name)}</title>")
    w(f"<style>{_CSS}</style>")
    w("</head><body>")
    w('<div class="sheet">')
    w('<div class="toolbar"><button type="button" onclick="window.print()">'
      "Save as PDF</button></div>")

    _cover(w, report, kind)
    _summary(w, report)
    _caveats(w, report)
    # Numbered only when there is a sequence to follow; a lone section needs no "1.".
    sections = [
        (_assessment, True),
        (_plan, not assessment_only),
        (_result, not assessment_only),
        (_validation, not assessment_only),
        (_parity, not assessment_only),
    ]
    included = [render for render, on in sections if on]
    for i, render in enumerate(included, start=1):
        render(w, report, f"{i}. " if len(included) > 1 else "")
    _footer(w, report, kind)

    w("</div></body></html>")
    return "\n".join(out)


def _cover(w, report: MigrationReport, kind: str) -> None:
    p = report.provenance
    c = report.coordinates
    w('<header class="cover">')
    w(f'<div class="cover__brand">Lakebase Express · {_e(kind)}</div>')
    w(f"<h1>{_e(p.project_name)}</h1>")
    w(f'<p class="cover__sub">{_e(c.source_database or "Source")} '
      f"→ Databricks Lakebase · generated {_e(_date(p.generated_at))}</p>")
    w("</header>")

    w('<div class="flow">')
    w('<div class="flow__box">')
    w('<div class="flow__label">Source</div>')
    w(f"<div><strong>{_e(c.source_database or '—')}</strong></div>")
    w(f'<div class="muted small">{_e(c.source_host or "host not recorded")}'
      f"{f' · {_e(c.source_type)}' if c.source_type else ''}</div>")
    w("</div>")
    w('<div class="flow__box">')
    w('<div class="flow__label">Target — Lakebase (PostgreSQL)</div>')
    w(f"<div><strong>{_e(c.target_database or '—')}</strong>"
      f"{f' · schema <code>{_e(c.target_schema)}</code>' if c.target_schema else ''}</div>")
    w(f'<div class="muted small">{_e(c.target_host or "host not recorded")} · identifiers '
      f"{_e(c.identifier_case or 'lowercase')}</div>")
    w("</div>")
    w("</div>")


def _summary(w, report: MigrationReport) -> None:
    """Scores, then counts. Only what the scope covers: a plan-item or rows-copied tile
    in an assessment-only export would be reporting on work that has not started."""
    h = report.headline
    assessment_only = report.scope == SCOPE_ASSESSMENT
    scores = [(h.readiness_score, "Readiness", "/100")]
    if not assessment_only:
        scores += [(h.match_score, "Validation match", "%"),
                   (h.parity_score, "Query parity", "%")]

    w('<div class="scores">')
    for value, label, suffix in scores:
        if value is None:
            w('<div class="score"><div class="score__n score__n--na">Not run</div>'
              f'<div class="score__l">{_e(label)}</div></div>')
        else:
            w(f'<div class="score"><div class="score__n">{value}{_e(suffix)}</div>'
              f'<div class="score__l">{_e(label)}</div></div>')
    w("</div>")

    counts = [
        (h.tables, "Source tables"),
        (h.total_rows, "Source rows"),
        (h.programmable_objects, "Code objects"),
    ]
    counts += (
        [(report.assessment.findings_total if report.assessment else 0, "Findings")]
        if assessment_only
        else [(h.plan_items, "Plan items"), (h.rows_copied, "Rows copied")]
    )
    w('<div class="scores">')
    for value, label in counts:
        w(f'<div class="score"><div class="score__n">{_n(value)}</div>'
          f'<div class="score__l">{_e(label)}</div></div>')
    w("</div>")


def _caveats(w, report: MigrationReport) -> None:
    notes = report.provenance.completeness
    if not notes:
        return
    w('<div class="note note--warn">')
    w("<strong>What this report cannot vouch for</strong>")
    w("<ul>")
    for note in notes:
        w(f"<li>{_e(note)}</li>")
    w("</ul></div>")


def _assessment(w, report: MigrationReport, n: str = "") -> None:
    w(f"<h2>{_e(n)}Assessment of the source</h2>")
    section = report.assessment
    if section is None:
        w('<p class="muted">The source was never scanned, so there is nothing to report.</p>')
        return

    w(f"<p>Scanned <strong>{_e(section.database)}</strong>: "
      f"{_n(section.table_count)} tables, {_n(section.total_rows)} rows, and "
      f"{_n(section.programmable_object_count)} programmable objects"
      + (" ("
         + ", ".join(
             _e(_count(n, k.lower(), f"{k.lower()}s"))
             for k, n in sorted(section.object_counts.items())
         )
         + ")" if section.object_counts else "")
      + ".</p>")

    counts = section.severity_counts
    w(f'<p>Readiness <strong>{section.readiness_score}/100</strong> from '
      f"{_n(section.findings_total)} compatibility findings: "
      + " ".join(
          _pill(f"{counts.get(sev, 0)} {sev}", tone)
          for sev, tone in (("high", "err"), ("medium", "warn"), ("low", ""), ("info", ""))
      )
      + "</p>")
    w(f'<p class="muted small">{_e(section.score_formula)}</p>')

    if section.largest_tables:
        w("<h3>Largest tables</h3>")
        w("<table><thead><tr><th>Table</th><th class='num'>Rows</th>"
          "<th class='num'>Columns</th><th>Primary key</th></tr></thead><tbody>")
        for table in section.largest_tables:
            w(f"<tr><td><code>{_e(table.name)}</code></td><td class='num'>{_n(table.rows)}</td>"
              f"<td class='num'>{table.columns}</td>"
              f"<td>{f'<code>{_e(table.primary_key)}</code>' if table.primary_key else '<span class=muted>none</span>'}</td></tr>")
        w("</tbody></table>")
        w(_more(len(section.largest_tables), section.tables_total, "tables",
                    "Assessment"))

    if section.findings:
        w("<h3>Compatibility findings</h3>")
        w('<p class="muted small">Grouped by rule; a rule that fired on many objects is '
          "one entry naming them.</p>")
        for finding in section.findings:
            w(f'<div class="item item--{_e(finding.severity)}">')
            w('<div class="item__head">')
            w(f'<span class="item__title">{_e(finding.title)}</span>')
            w(_pill(finding.severity, _tone(finding.severity)))
            w(f'<span class="muted small"><code>{_e(finding.rule_id)}</code> · '
              f"{finding.affected_total} object(s)</span>")
            w("</div>")
            if finding.detail:
                w(f'<p class="item__body">{_e(finding.detail)}</p>')
            w("<dl>")
            if finding.recommendation:
                w(f"<dt>What to do</dt><dd>{_e(finding.recommendation)}</dd>")
            shown = ", ".join(f"<code>{_e(o)}</code>" for o in finding.affected)
            if finding.affected_total > len(finding.affected):
                shown += f" <span class='muted'>+{finding.affected_total - len(finding.affected)} more</span>"
            w(f"<dt>Affects</dt><dd>{shown}</dd>")
            w("</dl></div>")

    _ai_analysis(w, section.ai)


def _ai_analysis(w, ai) -> None:
    if ai is None:
        return
    w("<h3>AI migration analysis (advisory)</h3>")
    w(f'<div class="note"><strong>Written by <code>{_e(ai.endpoint)}</code> before the '
      "migration plan existed</strong>, so it may warn about risks the migration then "
      "handled, or suggest approaches it deliberately rejected. Everything else in this "
      "report is derived from what the phases recorded.</div>")
    if ai.summary:
        w(f"<p>{_e(ai.summary)}</p>")
    w(f"<p>Complexity <strong>{_e(ai.complexity)}</strong>"
      + (f" — {_e(ai.complexity_rationale)}" if ai.complexity_rationale else "")
      + (f" Estimated effort: {_e(ai.estimated_effort)}" if ai.estimated_effort else "")
      + "</p>")
    for risk in ai.risks:
        w(f'<div class="item item--{_e(risk.severity)}">')
        w('<div class="item__head">')
        w(f'<span class="item__title">{_e(risk.title)}</span>')
        w(_pill(risk.severity, _tone(risk.severity)))
        if risk.category:
            w(f'<span class="muted small">{_e(risk.category)}</span>')
        w("</div>")
        if risk.rationale:
            w(f'<p class="item__body">{_e(risk.rationale)}</p>')
        w("<dl>")
        if risk.affected_objects:
            w(f"<dt>Affects</dt><dd>{_e(risk.affected_objects)}</dd>")
        if risk.recommendation:
            w(f"<dt>Recommended</dt><dd>{_e(risk.recommendation)}</dd>")
        w("</dl></div>")
    if ai.recommendations:
        w("<p><strong>Recommendations</strong></p><ul>")
        for rec in ai.recommendations:
            w(f"<li>{_e(rec)}</li>")
        w("</ul>")


def _plan(w, report: MigrationReport, n: str = "") -> None:
    w(f"<h2>{_e(n)}Migration plan</h2>")
    section = report.plan
    if section is None:
        w('<p class="muted">No plan was built, so nothing states what the migration was '
          "going to create.</p>")
        return

    w(f"<p><strong>{_n(section.total)}</strong> objects planned — "
      f"{_n(section.pre_data)} applied before the data load and "
      f"{_n(section.post_data)} after it, so the copy pays no per-row constraint or "
      "index maintenance.</p>")
    if section.by_kind:
        w("<p>"
          + " ".join(
              _pill(f"{count} {kind}") for kind, count in sorted(section.by_kind.items())
          )
          + "</p>")
    if section.collations:
        w(f"<p>{len(section.collations)} source collation(s) mirrored as ICU collations: "
          + ", ".join(f"<code>{_e(c)}</code>" for c in section.collations)
          + ". String comparison keeps the source's case and accent semantics.</p>")

    if not section.code_objects:
        return
    w("<h3>Translated code objects</h3>")
    w(f'<p class="muted small">{_n(section.translated)} translated by a model, '
      f"{_n(section.user_edited)} edited by hand, {_n(section.not_translated)} not "
      "translated. <em>Now</em> is what the SQL actually creates, which differs from the "
      "source kind when a procedure returning rows had to become a function.</p>")
    w("<table><thead><tr><th>Source</th><th>Target</th><th>Type</th><th>Now</th>"
      "<th>Origin</th></tr></thead><tbody>")
    for row in section.code_objects:
        tone = "err" if row.provenance == "not-translated" else ""
        reshaped = row.target_kind and row.target_kind != row.object_type.lower()
        w(f"<tr><td><code>{_e(row.source)}</code></td>"
          f"<td><code>{_e(row.target)}</code></td>"
          f"<td>{_e(row.object_type.title())}</td>"
          f"<td>{_pill(row.target_kind or '—', 'warn' if reshaped else '')}</td>"
          f"<td>{_pill(_PROVENANCE_LABEL.get(row.provenance, row.provenance), tone)}</td></tr>")
    w("</tbody></table>")
    w(_more(len(section.code_objects), section.code_objects_total, "code objects",
                "Schema & Code plan"))


def _result(w, report: MigrationReport, n: str = "") -> None:
    w(f"<h2>{_e(n)}What the migration did</h2>")
    result = report.result
    if result is None or not result.runs:
        w('<p class="muted">No data load is recorded against this project.</p>')
        return

    loads = [r for r in result.runs if r.kind != "async_job"]
    provisioned = len(result.runs) - len(loads)
    w(f"<p><strong>{_n(result.rows_copied)}</strong> rows copied across "
      f"{_e(_count(result.tables_loaded, 'table', 'tables'))}, from "
      f"{_e(_count(len(loads), 'recorded load', 'recorded loads'))} — each table counted "
      "once, from the newest load that landed it, and a table that failed committed "
      "nothing so its rows are not counted."
      + (f" {_e(_count(provisioned, 'Databricks job was', 'Databricks jobs were'))} also "
         "provisioned for this project." if provisioned else "")
      + "</p>")
    w(_more(len(result.runs), result.runs_total, "recorded runs", "run history"))

    w("<table><thead><tr><th>Run</th><th>Status</th><th>Started</th><th>Took</th>"
      "<th class='num'>Tables</th><th class='num'>Rows</th></tr></thead><tbody>")
    for run in result.runs:
        # A provisioning record copied nothing itself, so its counts would read as a
        # load that moved no rows.
        provisioned = run.kind == "async_job"
        tables = "—" if provisioned else f"{run.tables_ok + run.tables_skipped}/{run.tables_total}"
        w(f"<tr><td>{_e(_KIND_LABEL.get(run.kind, run.kind))}"
          f"<br><span class='muted small'><code>{_e(run.run_id[:8])}</code>"
          + (f" · resumed {_e(run.resumed_from[:8])}" if run.resumed_from else "")
          + (f" · cron <code>{_e(run.scheduled_cron)}</code>" if run.scheduled_cron else "")
          + (f" · <a href='{_e(run.job_url)}'>job</a>" if run.job_url else "")
          + "</span></td>"
          f"<td>{_status_pill(run.status)}</td>"
          f"<td class='small'>{_e(_date(run.started_at) or '—')}</td>"
          f"<td class='small'>{_e(run.duration or '—')}</td>"
          f"<td class='num'>{_e(tables)}</td>"
          f"<td class='num'>{'—' if provisioned else _n(run.rows_copied)}</td></tr>")
    w("</tbody></table>")

    failed = [r for r in result.runs if r.error]
    for run in failed:
        w(f'<div class="item item--err"><div class="item__head">'
          f'<span class="item__title">{_e(_KIND_LABEL.get(run.kind, run.kind))} '
          f"<code>{_e(run.run_id[:8])}</code> reported an error</span></div>"
          f'<p class="item__body">{_e(run.error)}</p></div>')

    newest = next((r for r in result.runs if r.tables), None)
    if newest is not None:
        _run_tables(w, newest)

    if not result.history_persistent:
        w('<div class="note note--warn">Run history is not held in a durable store, so any '
          "load recorded before the last restart is missing from the table above.</div>")


def _run_tables(w, run: LoadRun) -> None:
    w(f"<h3>Tables in the most recent load "
      f"(<code>{_e(run.run_id[:8])}</code>)</h3>")
    w("<table><thead><tr><th>Source table</th><th>Target</th><th>Status</th>"
      "<th class='num'>Rows copied</th><th class='num'>Expected</th></tr></thead><tbody>")
    for table in run.tables:
        w(f"<tr><td><code>{_e(table.name)}</code></td>"
          f"<td>{f'<code>{_e(table.target)}</code>' if table.target else '—'}</td>"
          f"<td>{_status_pill(table.status)}"
          + (f"<br><span class='muted small'>{_e(table.error)}</span>" if table.error else "")
          + "</td>"
          f"<td class='num'>{_n(table.rows_copied)}</td>"
          f"<td class='num'>{_n(table.total_rows) if table.total_rows else '—'}</td></tr>")
    w("</tbody></table>")


def _validation(w, report: MigrationReport, n: str = "") -> None:
    w(f"<h2>{_e(n)}Validation — source against target</h2>")
    section = report.validation
    if section is None:
        w('<p class="muted">Validation has not been run, so nothing here verifies what is '
          "in the target.</p>")
        return

    w(f"<p>Matched <strong>{section.match_score}%</strong> of "
      f"{_n(section.total_source)} compared objects: "
      + " ".join((
          _pill(f"{section.matched} matched", "ok"),
          _pill(f"{section.missing} missing", "err" if section.missing else ""),
          _pill(f"{section.mismatched} mismatched", "warn" if section.mismatched else ""),
          _pill(f"{section.extra} extra", "warn" if section.extra else ""),
      ))
      + (f" Compared {_e(_date(section.generated_at))}." if section.generated_at else "")
      + "</p>")

    _row_totals(w, section)
    if section.remediated:
        w(f'<p class="muted small">{section.remediated} item(s) had a fix applied by the '
          "repair agent or by hand before this comparison.</p>")

    if not section.outstanding:
        w('<div class="note">Every compared object matched. No outstanding '
          "inconsistency was recorded.</div>")
        return

    w(f"<h3>Outstanding — {section.outstanding_total} object(s) did not match</h3>")
    for row in section.outstanding:
        w(f'<div class="item item--{_tone(row.status) or "warn"}">')
        w('<div class="item__head">')
        w(f'<span class="item__title"><code>{_e(row.name)}</code></span>')
        w(_status_pill(row.status))
        w(_pill(row.kind))
        if row.severity:
            w(_pill(row.severity, _tone(row.severity)))
        w("</div>")
        if row.detail:
            w(f'<p class="item__body">{_e(row.detail)}</p>')
        w("<dl>")
        if row.source_rows is not None or row.target_rows is not None:
            approx = " (planner estimate)" if row.rows_approximate else ""
            w(f"<dt>Rows</dt><dd>source {_n(row.source_rows)} · target "
              f"{_n(row.target_rows)}{_e(approx)}</dd>")
        for label, values in (
            ("Columns missing", row.columns_missing),
            ("Columns extra", row.columns_extra),
            ("Type drift", row.type_drift),
            ("Collation drift", row.collation_drift),
            ("Objects", row.objects),
        ):
            if values:
                w(f"<dt>{_e(label)}</dt><dd>"
                  + ", ".join(f"<code>{_e(v)}</code>" for v in values)
                  + "</dd>")
        if row.recommendation:
            w(f"<dt>What to do</dt><dd>{_e(row.recommendation)}</dd>")
        w("</dl></div>")
    w(_more(len(section.outstanding), section.outstanding_total, "objects",
                "Validation report"))


def _row_totals(w, section: ValidationSection) -> None:
    if not section.tables_compared:
        return
    delta = section.row_delta
    tone = "ok" if delta == 0 else "err"
    label = "identical" if delta == 0 else f"{delta:+,} in the target"
    w(f"<p>Row totals over the {_n(section.tables_compared)} tables counted on both "
      f"sides: source {_n(section.source_rows)}, target {_n(section.target_rows)} — "
      f"{_pill(label, tone)}"
      + (f" {section.tables_estimated} of those were counted by planner estimate."
         if section.tables_estimated else "")
      + "</p>")


def _parity(w, report: MigrationReport, n: str = "") -> None:
    w(f"<h2>{_e(n)}Query parity — the same question asked of both</h2>")
    section = report.parity
    if section is None:
        w('<p class="muted">Query parity has not been run, so no behavioural difference '
          "has been proven by execution.</p>")
        return

    w(f"<p><strong>{section.parity_score}%</strong> of {_n(section.total)} generated "
      "read-only query pairs agreed on both sides: "
      + " ".join((
          _pill(f"{section.matched} match", "ok"),
          _pill(f"{section.mismatched} mismatch", "warn" if section.mismatched else ""),
          _pill(f"{section.errored} error", "err" if section.errored else ""),
      ))
      + "</p>")
    w(f'<p class="muted small">Generated by <code>{_e(section.endpoint or "a model")}</code>'
      + (f" on {_e(_date(section.generated_at))}" if section.generated_at else "")
      + ". Each pair is the same intent written in T-SQL and in PostgreSQL, run against "
      "both databases and compared on row count, result shape, and duration.</p>")
    _parity_timing(w, section)

    if not section.queries:
        return
    w("<h3>Every compared pair</h3>")
    w("<table><thead><tr><th>Query</th><th>Status</th><th class='num'>Rows "
      "src/tgt</th><th class='num'>Source</th><th class='num'>Target</th></tr>"
      "</thead><tbody>")
    for row in section.queries:
        rows_tone = "" if row.count_match else "warn"
        w(f"<tr><td><strong>{_e(row.title or row.id)}</strong>"
          + (f" <span class='pill'>{_e(row.category)}</span>" if row.category else "")
          + (f"<br><span class='muted small'>{_e(row.detail)}</span>" if row.detail else "")
          + ("<br><span class='muted small'>differs on "
             + ", ".join(f"<code>{_e(c)}</code>" for c in row.mismatch_columns)
             + "</span>" if row.mismatch_columns else "")
          + (f"<br><span class='muted small'>source error: {_e(row.source_error)}</span>"
             if row.source_error else "")
          + (f"<br><span class='muted small'>target error: {_e(row.target_error)}</span>"
             if row.target_error else "")
          + "</td>"
          f"<td>{_status_pill(row.status)}</td>"
          f"<td class='num'>{_pill(f'{row.source_rows} / {row.target_rows}', rows_tone)}</td>"
          f"<td class='num'>{_e(_ms(row.source_ms))}</td>"
          f"<td class='num'>{_e(_ms(row.target_ms))}</td></tr>")
    w("</tbody></table>")


def _parity_timing(w, section: ParitySection) -> None:
    if section.speedup is None:
        return
    faster = section.speedup >= 1
    if 0.95 <= section.speedup <= 1.05:
        verdict = "comparable"
    elif faster:
        verdict = f"{section.speedup}× faster on Lakebase"
    else:
        verdict = f"{round(1 / section.speedup, 2)}× slower on Lakebase"
    w(f"<p>Total execution time over the pairs that ran on both sides: source "
      f"{_e(_ms(section.source_total_ms))}, target {_e(_ms(section.target_total_ms))} — "
      f"{_pill(verdict, 'ok' if faster else 'warn')}. Indicative only: the two databases "
      "were not under comparable load and neither was warmed.</p>")


def _footer(w, report: MigrationReport, kind: str) -> None:
    p = report.provenance
    assessment_only = report.scope == SCOPE_ASSESSMENT
    w("<footer>")
    w(f"<p>{_e(kind)} generated {_e(p.generated_at)} by {_e(p.tool)} "
      f"{_e(p.tool_version)} (report v{_e(p.report_version)}) from migration project "
      f"<code>{_e(p.project_id)}</code>.</p>")
    if assessment_only:
        w("<p>Covers the source assessment only — the Migration Report module exports the "
          "same project with the plan, the load, validation and query parity.</p>")
    elif p.phase_statuses:
        # Only on the full report: which phases had run is what dates a mid-migration
        # export, and an assessment-only one is not claiming to describe them.
        w("<p>Phase status at export: "
          + " ".join(
              _pill(f"{phase} {status.replace('_', ' ')}",
                    "ok" if status == "done" else "warn" if status == "in_progress" else "")
              for phase, status in sorted(p.phase_statuses.items())
          )
          + "</p>")
    w("<p>Derived entirely from what the migration phases recorded; no database was "
      "queried, and no password or secret value is included.</p>")
    w("</footer>")
