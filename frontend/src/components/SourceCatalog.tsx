import { useMemo, useState } from "react";
import { SOURCE_CONNECTORS, type Connector } from "../connectors";
import ConnectorIcon from "./ConnectorIcon";

export default function SourceCatalog({ onSelect }: { onSelect: (c: Connector) => void }) {
  const [q, setQ] = useState("");

  const filtered = useMemo(() => {
    const t = q.trim().toLowerCase();
    const matches = (c: Connector) =>
      !t || c.name.toLowerCase().includes(t) || c.category.toLowerCase().includes(t);
    // Enabled first, then alphabetical — mirrors Airbyte's "available" sort.
    return SOURCE_CONNECTORS.filter(matches).sort(
      (a, b) => Number(b.enabled) - Number(a.enabled) || a.name.localeCompare(b.name),
    );
  }, [q]);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Select a source</h1>
          <p className="muted">Choose the database to migrate into Databricks Lakebase.</p>
        </div>
        <div className="search-wrap">
          <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2">
            <circle cx="11" cy="11" r="7" />
            <path d="m20 20-3.5-3.5" />
          </svg>
          <input
            className="search"
            placeholder="Search connectors…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </div>
      </div>

      <div className="catalog">
        {filtered.map((c) => (
          <button
            key={c.id}
            className={`ctile ${c.enabled ? "" : "ctile--disabled"}`}
            disabled={!c.enabled}
            onClick={() => c.enabled && onSelect(c)}
          >
            <div className="ctile__top">
              <ConnectorIcon connector={c} size={44} />
              <span className={`badge ${c.enabled ? "badge--ok" : "badge--soon"}`}>
                {c.enabled ? "Available" : "Coming soon"}
              </span>
            </div>
            <div className="ctile__name">{c.name}</div>
            <div className="ctile__cat">{c.category}</div>
            <div className="ctile__desc">{c.description}</div>
          </button>
        ))}
      </div>
    </>
  );
}
