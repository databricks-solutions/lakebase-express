export type NavId = "migrations" | "settings";

const ICONS: Record<NavId, JSX.Element> = {
  migrations: (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2">
      <ellipse cx="6" cy="6" rx="3.5" ry="2.5" />
      <ellipse cx="18" cy="18" rx="3.5" ry="2.5" />
      <path d="M9 6h4a4 4 0 0 1 4 4v5" />
      <path d="M14.5 12.5 17 15l2.5-2.5" />
    </svg>
  ),
  settings: (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2">
      <circle cx="12" cy="12" r="3" />
      <path d="M19 12a7 7 0 0 0-.1-1l2-1.5-2-3.4-2.3 1a7 7 0 0 0-1.7-1l-.3-2.6H10l-.3 2.6a7 7 0 0 0-1.7 1l-2.3-1-2 3.4 2 1.5a7 7 0 0 0 0 2l-2 1.5 2 3.4 2.3-1a7 7 0 0 0 1.7 1l.3 2.6h3.4l.3-2.6a7 7 0 0 0 1.7-1l2.3 1 2-3.4-2-1.5a7 7 0 0 0 .1-1z" />
    </svg>
  ),
};

const ITEMS: { id: NavId; label: string }[] = [
  { id: "migrations", label: "Migrations" },
  { id: "settings", label: "Settings" },
];

interface Props {
  active: NavId;
  onNavigate: (id: NavId) => void;
  onNew: () => void;
}

export default function Sidebar({ active, onNavigate, onNew }: Props) {
  return (
    <aside className="sidebar">
      <div className="sidebar__new">
        <button className="btn btn--primary" style={{ width: "100%", justifyContent: "center" }} onClick={onNew}>
          <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2.4">
            <path d="M12 5v14M5 12h14" />
          </svg>
          New migration
        </button>
      </div>

      <nav className="sidebar__nav">
        <div className="navlabel">Workspace</div>
        {ITEMS.map((it) => (
          <button
            key={it.id}
            className={`navitem ${active === it.id ? "navitem--active" : ""}`}
            onClick={() => onNavigate(it.id)}
          >
            {ICONS[it.id]}
            <span>{it.label}</span>
          </button>
        ))}
      </nav>

      <div className="sidebar__footer">
        <span className="dot dot--ok" /> Target: Databricks Lakebase
      </div>
    </aside>
  );
}
