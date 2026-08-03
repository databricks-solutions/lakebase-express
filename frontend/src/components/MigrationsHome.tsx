import { useEffect, useMemo, useState } from "react";
import { api, type PhaseStatus, type ProjectSummary } from "../api";
import { SOURCE_CONNECTORS, LAKEBASE_DESTINATION } from "../connectors";
import ConnectorIcon from "./ConnectorIcon";

const PHASES = ["assessment", "sizing", "schema", "data"] as const;
const PHASE_LABEL: Record<string, string> = {
  assessment: "Assess", sizing: "Size", schema: "Schema", data: "Data",
};

interface Props {
  query: string;
  onNew: () => void;
  onOpen: (id: string) => void;
}

export default function MigrationsHome({ query, onNew, onOpen }: Props) {
  const [projects, setProjects] = useState<ProjectSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sourceFilter, setSourceFilter] = useState("all");

  function reload() {
    api.listProjects().then(setProjects).catch((e) => setError((e as Error).message));
  }
  useEffect(reload, []);

  async function del(id: string, e: React.MouseEvent) {
    e.stopPropagation();
    if (!confirm("Delete this migration project? This cannot be undone.")) return;
    setError(null);
    try {
      await api.deleteProject(id);
      reload();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  const filtered = useMemo(() => {
    if (!projects) return null;
    const q = query.trim().toLowerCase();
    return projects.filter((p) => {
      const src = SOURCE_CONNECTORS.find((c) => c.id === p.source_connector_id);
      const matchesQuery = !q || p.name.toLowerCase().includes(q) || (src?.name.toLowerCase().includes(q) ?? false);
      const matchesSource = sourceFilter === "all" || p.source_connector_id === sourceFilter;
      return matchesQuery && matchesSource;
    });
  }, [projects, query, sourceFilter]);

  // Sources that actually appear in the project list, for the filter dropdown.
  const usedSources = useMemo(() => {
    const ids = new Set((projects ?? []).map((p) => p.source_connector_id));
    return SOURCE_CONNECTORS.filter((c) => ids.has(c.id));
  }, [projects]);

  return (
    <>
      <div className="page-head">
        <div>
          <div className="page-head__crumbs">Workspace / Migrations</div>
          <h1>
            <span className="titleicon">
              <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2">
                <ellipse cx="6" cy="6" rx="3.5" ry="2.5" />
                <ellipse cx="18" cy="18" rx="3.5" ry="2.5" />
                <path d="M9 6h4a4 4 0 0 1 4 4v5" />
                <path d="M14.5 12.5 17 15l2.5-2.5" />
              </svg>
            </span>
            Migrations
          </h1>
          <p className="muted">Each migration is a saved project you can configure, run, and resume.</p>
        </div>
        <div className="page-head__actions">
          <button className="btn btn--primary" onClick={onNew}>
            <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2.4">
              <path d="M12 5v14M5 12h14" />
            </svg>
            New migration
          </button>
        </div>
      </div>

      {projects && projects.length > 0 && (
        <div className="filterbar">
          <select value={sourceFilter} onChange={(e) => setSourceFilter(e.target.value)}>
            <option value="all">All sources</option>
            {usedSources.map((c) => (
              <option key={c.id} value={c.id}>{c.name}</option>
            ))}
          </select>
          <span className="filterbar__count">
            {filtered?.length ?? 0} of {projects.length} migration{projects.length === 1 ? "" : "s"}
          </span>
        </div>
      )}

      <div className="tablewrap">
        {error && <div className="banner banner--err">{error}</div>}
        {projects === null ? (
          <p className="muted">Loading…</p>
        ) : projects.length === 0 ? (
          <div className="empty">
            <h2>No migrations yet</h2>
            <p className="muted">Start a migration from a source database into Databricks Lakebase.</p>
            <button className="btn btn--primary btn--lg" onClick={onNew}>+ New migration</button>
          </div>
        ) : filtered && filtered.length === 0 ? (
          <div className="empty">
            <h2>No matches</h2>
            <p className="muted">No migrations match your search and filters.</p>
          </div>
        ) : (
          <table className="dtable">
            <thead>
              <tr>
                <th>Name</th>
                <th>Source → Target</th>
                <th>Progress</th>
                <th>Last updated</th>
                <th className="dtable__actions"></th>
              </tr>
            </thead>
            <tbody>
              {filtered!.map((p) => {
                const src = SOURCE_CONNECTORS.find((c) => c.id === p.source_connector_id);
                return (
                  <tr key={p.id} onClick={() => onOpen(p.id)}>
                    <td className="dtable__namecol">
                      <div className="dtable__name">
                        {src && <ConnectorIcon connector={src} size={28} />}
                        {p.name}
                      </div>
                    </td>
                    <td className="dtable__sub">
                      {src?.name ?? p.source_connector_id} → {LAKEBASE_DESTINATION.name}
                      {p.target_host ? ` (${p.target_host})` : ""}
                    </td>
                    <td>
                      <span className="phasebar">
                        {PHASES.map((ph) => (
                          <span
                            key={ph}
                            className={`phasedot phasedot--${statusClass(p.statuses[ph])}`}
                            title={`${PHASE_LABEL[ph]}: ${p.statuses[ph] ?? "not started"}`}
                          >
                            {PHASE_LABEL[ph]}
                          </span>
                        ))}
                      </span>
                    </td>
                    <td className="dtable__sub">{new Date(p.updated_at).toLocaleString()}</td>
                    <td className="dtable__actions">
                      <button className="iconbtn" title="Delete" onClick={(e) => del(p.id, e)}>✕</button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}

function statusClass(s: PhaseStatus | undefined): string {
  return s === "done" ? "done" : s === "in_progress" ? "prog" : "todo";
}
