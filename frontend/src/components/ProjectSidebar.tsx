import { LAKEBASE_DESTINATION, type Connector } from "../connectors";
import ConnectorIcon from "./ConnectorIcon";

export type ModuleId =
  | "overview"
  | "connection"
  | "assessment"
  | "schema"
  | "data"
  | "sync"
  | "validation"
  | "parity";

// Ordered migration journey. `step` 0 = the project hub; 1..5 = guided sequence.
// `group: "post"` marks independent post-migration modules — rendered in their
// own sidebar section, outside the numbered journey.
export const MODULES: { id: ModuleId; label: string; desc: string; step: number; group?: "post" }[] = [
  { id: "overview", label: "Overview", desc: "The migration project — journey, status, and next step.", step: 0 },
  { id: "connection", label: "Connections & Target", desc: "Configure and test the source and Lakebase target.", step: 1 },
  { id: "assessment", label: "Assessment", desc: "Scan the source, review compatibility, and estimate Lakebase sizing & cost.", step: 2 },
  { id: "schema", label: "Schema & Code", desc: "Generate and review the AI migration plan — DDL and translated Postgres code.", step: 3 },
  { id: "data", label: "Data Migration", desc: "Select the tables and load options to migrate.", step: 4 },
  { id: "sync", label: "Create Sync", desc: "Run the migration now in-app, or offload it to a re-runnable (optionally scheduled) Databricks snapshot job.", step: 5 },
  { id: "validation", label: "Validation", desc: "Compare source and Lakebase — object coverage, row counts, and structure — then let the AI repair agent resolve inconsistencies, or fix them manually.", step: 0, group: "post" },
  { id: "parity", label: "Query Parity", desc: "Generate synthetic read-only queries, run them against source and Lakebase, and compare row count, result format, and performance.", step: 0, group: "post" },
];

interface Props {
  projectName: string;
  source: Connector;
  active: ModuleId;
  onSelect: (id: ModuleId) => void;
  onBack: () => void;
  done: Record<ModuleId, boolean>;
}

export default function ProjectSidebar({ projectName, source, active, onSelect, onBack, done }: Props) {
  return (
    <aside className="sidebar">
      <div className="projhead">
        <button className="link link--light" onClick={onBack}>← Migrations</button>
        <div className="projhead__name">{projectName}</div>
        <div className="projhead__flow">
          <ConnectorIcon connector={source} size={22} />
          <span className="projhead__arrow">→</span>
          <ConnectorIcon connector={LAKEBASE_DESTINATION} size={22} />
        </div>
      </div>

      <nav className="sidebar__nav">
        <div className="navlabel">Migration journey</div>
        {MODULES.filter((m) => !m.group).map((m) => (
          <NavItem key={m.id} module={m} active={active} onSelect={onSelect} done={done} />
        ))}
        <div className="navlabel">Post-migration</div>
        {MODULES.filter((m) => m.group === "post").map((m) => (
          <NavItem key={m.id} module={m} active={active} onSelect={onSelect} done={done} />
        ))}
      </nav>
    </aside>
  );
}

function NavItem({
  module: m,
  active,
  onSelect,
  done,
}: {
  module: (typeof MODULES)[number];
  active: ModuleId;
  onSelect: (id: ModuleId) => void;
  done: Record<ModuleId, boolean>;
}) {
  const marker = m.group === "post" ? "⇄" : "•";
  return (
    <button
      className={`navitem ${active === m.id ? "navitem--active" : ""}`}
      onClick={() => onSelect(m.id)}
    >
      {m.step > 0 ? <span className="navnum">{m.step}</span> : <span className="navnum navnum--hub">{marker}</span>}
      <span>{m.label}</span>
      {done[m.id] && <span className="navcheck">✓</span>}
    </button>
  );
}
