import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type ObjectKind, type PlanItem, type PlanRunState } from "../api";
import type { MigrationState } from "../App";
import { TranslatingStatus } from "../components/Progress";

interface Props {
  state: MigrationState;
  setState: React.Dispatch<React.SetStateAction<MigrationState>>;
  fmEndpoint?: string;
}

const KIND_PLURAL: Record<ObjectKind, string> = {
  schema: "Schemas", collation: "Collations", table: "Tables", function: "Functions",
  view: "Views", procedure: "Procedures",
  constraint: "Constraints", index: "Indexes", foreign_key: "Foreign keys",
  trigger: "Triggers",
};
// Display order mirrors the dependency-apply order (collations precede their
// tables; constraints, indexes, FKs, and triggers are post-data).
const KIND_ORDER: ObjectKind[] = [
  "schema", "collation", "table", "function", "view", "procedure",
  "constraint", "index", "foreign_key", "trigger",
];

// Rows rendered per group before "Show more" — keeps the DOM bounded at scale.
const PAGE = 150;
// Below this many objects, expand every group by default (small plans).
const AUTO_OPEN_MAX = 40;

export default function SchemaCode({ state, setState, fmEndpoint }: Props) {
  const report = state.report;
  const items = state.plan; // persisted on the project
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Translation progress from the background plan run (objects done / total).
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const pollRef = useRef(true);
  useEffect(() => () => { pollRef.current = false; }, []);

  // View controls.
  const [query, setQuery] = useState("");
  const [kindFilter, setKindFilter] = useState<Set<ObjectKind>>(new Set());
  const [aiOnly, setAiOnly] = useState(false);
  const [openGroups, setOpenGroups] = useState<Set<ObjectKind>>(new Set());
  const [openItems, setOpenItems] = useState<Set<string>>(new Set());
  const [limits, setLimits] = useState<Record<string, number>>({});

  // Default which groups are open whenever a new plan arrives (small → all open).
  useEffect(() => {
    if (!items) return;
    const present = new Set(items.map((i) => i.kind));
    setOpenGroups(items.length <= AUTO_OPEN_MAX ? present : new Set());
    setOpenItems(new Set());
    setLimits({});
  }, [items?.length]); // eslint-disable-line react-hooks/exhaustive-deps

  // Reset pagination when the active filter changes.
  useEffect(() => setLimits({}), [query, aiOnly, kindFilter]);

  const buildPlan = useCallback(async () => {
    setBusy(true);
    setError(null);
    setProgress(null);
    pollRef.current = true;
    try {
      // Background run + poll: AI translation of the code objects runs well past
      // the Databricks Apps ~120s request timeout, so it can't be done inline.
      const { run_id } = await api.startBuildPlan({
        tables: report!.tables,
        programmable_objects: report!.programmable_objects,
        target_schema: state.targetSchema,
        identifier_case: state.identifierCase,
        translate: true, // T-SQL code objects are always AI-translated
        endpoint: fmEndpoint || undefined,
      });
      // Poll until the run finishes; ~1.2s cadence matches the other phases.
      for (;;) {
        await new Promise((r) => setTimeout(r, 1200));
        if (!pollRef.current) return;
        let st: PlanRunState;
        try {
          st = await api.planStatus(run_id);
        } catch {
          continue; // transient poll error — keep trying
        }
        if (!pollRef.current) return;
        if (st.objects_total > 0) setProgress({ done: st.objects_done, total: st.objects_total });
        if (st.status === "success") {
          setState((s) => ({ ...s, plan: st.items ?? [] }));
          break;
        }
        if (st.status === "failed") {
          setError(st.error || "Plan generation failed.");
          break;
        }
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
      setProgress(null);
    }
  }, [report, state.targetSchema, state.identifierCase, fmEndpoint, setState]);

  const editSql = useCallback(
    (id: string, sql: string) =>
      setState((s) => ({ ...s, plan: s.plan?.map((it) => (it.id === id ? { ...it, sql } : it)) ?? null })),
    [setState],
  );
  const toggleItem = useCallback(
    (id: string) =>
      setOpenItems((prev) => {
        const next = new Set(prev);
        next.has(id) ? next.delete(id) : next.add(id);
        return next;
      }),
    [],
  );

  // Counts per kind across the whole plan (for the filter chips).
  const kindCounts = useMemo(() => {
    const c = {} as Record<ObjectKind, number>;
    for (const it of items ?? []) c[it.kind] = (c[it.kind] ?? 0) + 1;
    return c;
  }, [items]);
  const aiTotal = useMemo(() => (items ?? []).filter((i) => i.original).length, [items]);

  // Apply search + kind filter + AI-only, then bucket by kind.
  const { grouped, shownCount } = useMemo(() => {
    const q = query.trim().toLowerCase();
    const grouped = {} as Record<ObjectKind, PlanItem[]>;
    let shownCount = 0;
    for (const it of items ?? []) {
      if (kindFilter.size && !kindFilter.has(it.kind)) continue;
      if (aiOnly && !it.original) continue;
      if (q && !it.name.toLowerCase().includes(q)) continue;
      (grouped[it.kind] ??= []).push(it);
      shownCount++;
    }
    return { grouped, shownCount };
  }, [items, query, kindFilter, aiOnly]);

  const filtering = !!query.trim() || kindFilter.size > 0 || aiOnly;

  function toggleKind(k: ObjectKind) {
    setKindFilter((prev) => {
      const next = new Set(prev);
      next.has(k) ? next.delete(k) : next.add(k);
      return next;
    });
  }
  function toggleGroup(k: ObjectKind) {
    setOpenGroups((prev) => {
      const next = new Set(prev);
      next.has(k) ? next.delete(k) : next.add(k);
      return next;
    });
  }
  const allKinds = KIND_ORDER.filter((k) => (kindCounts[k] ?? 0) > 0);
  const expandAll = () => setOpenGroups(new Set(allKinds));
  const collapseAll = () => setOpenGroups(new Set());

  if (!report) return <div className="card"><p className="muted">Run the assessment first.</p></div>;

  return (
    <div className="stack">
      <section className="card">
        <div className="card__head">
          <h2>Migration plan — schema &amp; code</h2>
          {items && (
            <span className="muted">
              {items.length.toLocaleString()} objects{aiTotal > 0 ? ` · ${aiTotal.toLocaleString()} AI-translated` : ""}
            </span>
          )}
        </div>
        <p className="muted">
          An AI migration agent translates each T-SQL procedure, view, function, and trigger into
          Postgres / PL-pgSQL; tables, constraints, and indexes become deterministic DDL. Review and
          edit any statement here — applying it to Lakebase happens in the Sync step. Constraints,
          indexes, foreign keys, and triggers are applied <strong>after</strong> the data load, so
          the bulk copy doesn't pay index-maintenance or FK-validation costs.
        </p>

        <div className="actions">
          <button className="btn btn--primary" disabled={busy} onClick={buildPlan}>
            {busy ? "Agent is translating…" : items ? "Rebuild plan" : "Generate plan with AI"}
          </button>
        </div>
        <TranslatingStatus active={busy} />
        {busy && progress && progress.total > 0 && (
          <p className="muted" style={{ marginTop: 4 }}>
            Translated {progress.done} of {progress.total} code objects…
          </p>
        )}
        {error && <div className="banner banner--err">{error}</div>}

        {items && items.length > 0 && (
          <>
            <div className="plantools">
              <div className="search-wrap">
                <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2">
                  <circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" />
                </svg>
                <input
                  className="search"
                  placeholder="Search objects by name…"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                />
              </div>

              <div className="chipbar">
                {allKinds.map((k) => (
                  <button
                    key={k}
                    type="button"
                    className={`chip ${kindFilter.has(k) ? "is-active" : ""}`}
                    aria-pressed={kindFilter.has(k)}
                    onClick={() => toggleKind(k)}
                  >
                    {KIND_PLURAL[k]} <span className="chip__n">{kindCounts[k]}</span>
                  </button>
                ))}
                {aiTotal > 0 && (
                  <button
                    type="button"
                    className={`chip ${aiOnly ? "is-active" : ""}`}
                    aria-pressed={aiOnly}
                    onClick={() => setAiOnly((v) => !v)}
                  >
                    ✦ AI-translated <span className="chip__n">{aiTotal}</span>
                  </button>
                )}
              </div>

              <div className="plantools__right">
                {filtering && <span className="muted">{shownCount.toLocaleString()} of {items.length.toLocaleString()}</span>}
                <button className="link" onClick={expandAll}>Expand all</button>
                <button className="link" onClick={collapseAll}>Collapse all</button>
              </div>
            </div>

            {shownCount === 0 ? (
              <p className="muted plan-empty">No objects match your search and filters.</p>
            ) : (
              <div className="plangroups">
                {KIND_ORDER.map((kind) => {
                  const groupItems = grouped[kind];
                  if (!groupItems || groupItems.length === 0) return null;
                  const open = filtering || openGroups.has(kind);
                  const limit = limits[kind] ?? PAGE;
                  const shown = groupItems.slice(0, limit);
                  const hidden = groupItems.length - shown.length;
                  return (
                    <div className="plangroup" key={kind}>
                      <button className="plangroup__head" onClick={() => toggleGroup(kind)} aria-expanded={open}>
                        <Caret open={open} />
                        <span className="plangroup__title">{KIND_PLURAL[kind]}</span>
                        <span className="plangroup__count">{groupItems.length.toLocaleString()}</span>
                      </button>
                      {open && (
                        <div className="plangroup__items">
                          {shown.map((it) => (
                            <PlanRow
                              key={it.id}
                              item={it}
                              expanded={openItems.has(it.id)}
                              onToggle={toggleItem}
                              onEdit={editSql}
                            />
                          ))}
                          {hidden > 0 && (
                            <button
                              className="plan-more"
                              onClick={() => setLimits((l) => ({ ...l, [kind]: limit + PAGE }))}
                            >
                              Show {Math.min(PAGE, hidden).toLocaleString()} more · {hidden.toLocaleString()} hidden
                            </button>
                          )}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </>
        )}
      </section>
    </div>
  );
}

/** One object row. The heavy editor body mounts only when expanded — critical
 *  for plans with thousands of objects. memo'd so editing one row doesn't
 *  re-render the rest. */
const PlanRow = memo(function PlanRow({
  item,
  expanded,
  onToggle,
  onEdit,
}: {
  item: PlanItem;
  expanded: boolean;
  onToggle: (id: string) => void;
  onEdit: (id: string, sql: string) => void;
}) {
  return (
    <div className={`planrow ${expanded ? "is-open" : ""}`}>
      <button className="planrow__head" onClick={() => onToggle(item.id)} aria-expanded={expanded}>
        <Caret open={expanded} small />
        <span className="planrow__name">{item.name}</span>
        {item.original && <span className="sbadge sbadge--ai">AI</span>}
        {!item.sql && <span className="sbadge sbadge--skip">empty</span>}
      </button>
      {expanded && (
        <div className="planrow__body">
          {item.notes && <p className="muted">{item.notes}</p>}
          {item.reasoning && (
            <details className="reasoning" open>
              <summary><span className="reasoning__icon" aria-hidden>✦</span> How the agent reasoned</summary>
              <div className="reasoning__body">{item.reasoning}</div>
            </details>
          )}
          <label className="muted">Postgres SQL (editable)</label>
          <textarea
            className="sqlbox"
            value={item.sql}
            placeholder="-- paste or edit the Postgres SQL to apply"
            onChange={(e) => onEdit(item.id, e.target.value)}
          />
          {item.original && (
            <details className="orig">
              <summary>Source T-SQL</summary>
              <pre className="code__body">{item.original}</pre>
            </details>
          )}
        </div>
      )}
    </div>
  );
});

function Caret({ open, small }: { open: boolean; small?: boolean }) {
  const d = small ? 16 : 18;
  return (
    <svg
      className={`caret ${open ? "caret--open" : ""}`}
      viewBox="0 0 24 24" width={d} height={d} fill="none" stroke="currentColor" strokeWidth="2"
      aria-hidden
    >
      <path d="m9 6 6 6-6 6" />
    </svg>
  );
}
