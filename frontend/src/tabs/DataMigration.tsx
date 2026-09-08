import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import type { TableInfo } from "../api";
import type { MigrationState } from "../App";
import { mapObject, mapSchema } from "../naming";

interface Props {
  state: MigrationState;
  setState: React.Dispatch<React.SetStateAction<MigrationState>>;
  onGoConnection?: () => void;
  onContinue?: () => void;
}

/** Data Migration is a *configuration* step: mark which tables to migrate and the
 *  load options. The migration is concluded (run now or scheduled) in Create Sync.
 *  Selection + options are persisted on the project so they carry forward. */
export default function DataMigration({ state, setState, onGoConnection, onContinue }: Props) {
  const report = state.report;
  const conn = state.connection;
  const [query, setQuery] = useState("");
  const initedRef = useRef(false);

  const tables = report?.tables ?? [];
  const key = (t: TableInfo) => `${t.schema_name}.${t.table_name}`;

  // First time on a fresh project, default to selecting everything.
  useEffect(() => {
    if (initedRef.current) return;
    initedRef.current = true;
    if (tables.length && state.selection.length === 0) {
      setState((s) => ({ ...s, selection: tables.map(key) }));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const dflt = state.targetSchema;
  const selected = useMemo(() => new Set(state.selection), [state.selection]);

  // Foreign-key graph over the scanned tables, keyed the same way as `selection`.
  // Self-references are dropped (a table's own rows satisfy them) and edges to
  // tables outside the scan are ignored — no selection here can satisfy those.
  const { parentsOf, childrenOf } = useMemo(() => {
    const known = new Set(tables.map(key));
    const parentsOf = new Map<string, Set<string>>();
    const childrenOf = new Map<string, Set<string>>();
    const link = (m: Map<string, Set<string>>, k: string, v: string) => {
      let set = m.get(k);
      if (!set) m.set(k, (set = new Set()));
      set.add(v);
    };
    for (const t of tables) {
      const child = key(t);
      for (const fk of t.foreign_keys ?? []) {
        const parent = `${fk.ref_schema}.${fk.ref_table}`;
        if (parent === child || !known.has(parent)) continue;
        link(parentsOf, child, parent);
        link(childrenOf, parent, child);
      }
    }
    return { parentsOf, childrenOf };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tables]);

  // Deselected tables that selected tables point at. Postgres validates existing
  // rows when a foreign key is added, so a loaded child against an unloaded (empty)
  // parent fails the post-data FK step — surfaced here, while selection is editable.
  const fkGaps = useMemo(() => {
    const gaps = new Map<string, string[]>();
    for (const [parent, kids] of childrenOf) {
      if (selected.has(parent)) continue;
      const refs = [...kids].filter((c) => selected.has(c)).sort();
      if (refs.length) gaps.set(parent, refs);
    }
    return gaps;
  }, [childrenOf, selected]);

  const groups = useMemo(() => {
    const q = query.trim().toLowerCase();
    const by: Record<string, TableInfo[]> = {};
    for (const t of tables) {
      if (q && !`${t.schema_name}.${t.table_name}`.toLowerCase().includes(q)) continue;
      (by[t.schema_name] ??= []).push(t);
    }
    return Object.entries(by).sort(([a], [b]) => a.localeCompare(b));
  }, [tables, query]);
  const visibleKeys = useMemo(() => groups.flatMap(([, ts]) => ts.map(key)), [groups]);

  if (!report || !conn) return <div className="card"><p className="muted">Run the assessment first.</p></div>;

  const selectedTables = tables.filter((t) => selected.has(key(t)));
  const selectedRows = selectedTables.reduce((n, t) => n + t.row_count, 0);

  const toggle = (k: string) =>
    setState((s) => ({
      ...s,
      selection: s.selection.includes(k) ? s.selection.filter((x) => x !== k) : [...s.selection, k],
    }));
  const setMany = (keys: string[], on: boolean) =>
    setState((s) => {
      const set = new Set(s.selection);
      keys.forEach((k) => (on ? set.add(k) : set.delete(k)));
      return { ...s, selection: [...set] };
    });
  /** Unselected FK ancestors of `roots`, walked transitively (cycle-safe). */
  const missingParents = (roots: string[]) => {
    const out = new Set<string>();
    const seen = new Set(roots);
    const stack = [...roots];
    while (stack.length) {
      for (const p of parentsOf.get(stack.pop()!) ?? []) {
        if (seen.has(p)) continue;
        seen.add(p);
        stack.push(p);
        if (!selected.has(p)) out.add(p);
      }
    }
    return [...out];
  };
  // Pull a referenced table in together with whatever it references in turn, so
  // one click can't leave a fresh gap behind.
  const includeWithParents = (keys: string[]) =>
    setMany([...new Set([...keys, ...missingParents(keys)])], true);
  // Everything the selection transitively needs but doesn't have yet.
  const fkClosure = fkGaps.size ? missingParents([...selected]) : [];

  const setOpt = (patch: Partial<MigrationState["dataOptions"]>) =>
    setState((s) => ({ ...s, dataOptions: { ...s.dataOptions, ...patch } }));

  return (
    <div className="stack">
      <section className="card">
        <div className="card__head">
          <h2>Select tables &amp; options</h2>
          <span className="muted">
            {selectedTables.length.toLocaleString()} of {tables.length.toLocaleString()} tables · {selectedRows.toLocaleString()} rows
          </span>
        </div>
        <p className="muted">
          Choose which tables to migrate and how to load them. Each table lands in its mapped schema
          according to the project's identifier-casing policy. You'll run or schedule the
          migration in the <strong>Create Sync</strong> step — nothing is moved here.
        </p>

        <div className="plantools">
          <div className="search-wrap">
            <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" />
            </svg>
            <input className="search" placeholder="Search tables…" value={query} onChange={(e) => setQuery(e.target.value)} />
          </div>
          <div className="plantools__right">
            <button className="link" onClick={() => setMany(visibleKeys, true)}>Select all</button>
            <button className="link" onClick={() => setMany(visibleKeys, false)}>Clear</button>
          </div>
        </div>

        {fkGaps.size > 0 && (
          <div className="banner banner--warn fkbanner">
            <span>
              <strong>{fkGaps.size.toLocaleString()}</strong> deselected{" "}
              {fkGaps.size === 1 ? "table is" : "tables are"} referenced by tables you are migrating.
              Foreign keys pointing at {fkGaps.size === 1 ? "it" : "them"} will fail to apply after the
              load — Postgres validates the loaded rows against a parent that stays empty.
              {fkClosure.length > fkGaps.size &&
                " Including them pulls in what they reference in turn."}
            </span>
            <button className="link fkbanner__fix" onClick={() => setMany(fkClosure, true)}>
              Include {fkClosure.length.toLocaleString()} referenced{" "}
              {fkClosure.length === 1 ? "table" : "tables"}
            </button>
          </div>
        )}

        <div className="tablelist">
          {groups.length === 0 && <div className="trow"><span className="muted">No tables match your search.</span></div>}
          {groups.map(([schema, ts]) => {
            const keys = ts.map(key);
            const selCount = keys.filter((k) => selected.has(k)).length;
            return (
              <div key={schema} className="tgroup">
                <div className="tgroup__head">
                  <GroupCheck
                    checked={selCount === keys.length}
                    indeterminate={selCount > 0 && selCount < keys.length}
                    onChange={(on) => setMany(keys, on)}
                  />
                  <span className="tgroup__title">
                    {schema} <span className="tgroup__arrow">→</span> {mapSchema(schema, dflt, state.identifierCase)}
                  </span>
                  <span className="tgroup__count">{selCount}/{ts.length}</span>
                </div>
                {ts.map((t) => {
                  const k = key(t);
                  const refs = fkGaps.get(k);
                  return (
                    <Fragment key={k}>
                      <label className="trow">
                        <input type="checkbox" checked={selected.has(k)} onChange={() => toggle(k)} />
                        <span className="trow__name">{t.table_name}</span>
                        <span className="trow__target">
                          {mapSchema(schema, dflt, state.identifierCase)}.{mapObject(t.table_name, state.identifierCase)}
                        </span>
                        <span className="trow__rows">{t.row_count.toLocaleString()} rows</span>
                      </label>
                      {refs && (
                        <div className="fkgap">
                          <span>
                            {refs.length.toLocaleString()} selected{" "}
                            {refs.length === 1 ? "table references" : "tables reference"} this —{" "}
                            <span className="fkgap__refs">{refList(refs)}</span>
                          </span>
                          <button className="link fkgap__fix" onClick={() => includeWithParents([k])}>
                            Include {t.table_name}
                          </button>
                        </div>
                      )}
                    </Fragment>
                  );
                })}
              </div>
            );
          })}
        </div>

        <div className="runbar">
          <label className="check">
            <input
              type="checkbox"
              checked={state.dataOptions.truncate_first}
              onChange={(e) => setOpt({ truncate_first: e.target.checked })}
            />
            Truncate target before load
          </label>
          <div className="field field--narrow">
            <label>Batch size</label>
            <input
              type="number"
              value={state.dataOptions.batch_size}
              onChange={(e) => setOpt({ batch_size: Number(e.target.value) })}
            />
          </div>
          <button className="btn btn--primary runbar__go" disabled={!selectedTables.length} onClick={onContinue}>
            Continue to Create Sync →
          </button>
        </div>

        <p className="savehint muted">
          Selections and options are saved automatically — concluded as <em>Sync now</em> or a
          <em> scheduled job</em> in Create Sync.
          {!state.lakebase?.host && onGoConnection && (
            <> The Lakebase target isn't set yet — <button className="link" onClick={onGoConnection}>configure it</button>.</>
          )}
        </p>
      </section>
    </div>
  );
}

/** Referencing tables, trimmed so a widely-referenced parent stays one line. */
function refList(keys: string[]): string {
  return keys.length <= 3
    ? keys.join(", ")
    : `${keys.slice(0, 3).join(", ")} +${keys.length - 3} more`;
}

/** Checkbox that supports the indeterminate (partial) state for group headers. */
function GroupCheck({ checked, indeterminate, onChange }: { checked: boolean; indeterminate: boolean; onChange: (on: boolean) => void }) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = indeterminate;
  }, [indeterminate]);
  return <input ref={ref} type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />;
}
