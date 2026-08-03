import { useState } from "react";
import { LAKEBASE_DESTINATION, type Connector } from "../connectors";
import ConnectorIcon from "./ConnectorIcon";
import SourceCatalog from "./SourceCatalog";

interface Props {
  onCancel: () => void;
  onCreate: (name: string, connectorId: string) => Promise<void>;
}

export default function NewMigration({ onCancel, onCreate }: Props) {
  const [source, setSource] = useState<Connector | null>(null);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!source) {
    return (
      <>
        <div className="topbar">
          <button className="link" onClick={onCancel}>← Migrations</button>
          <span className="muted">New migration · choose a source</span>
        </div>
        <SourceCatalog onSelect={(c) => { setSource(c); setName(`${c.name} → Lakebase`); }} />
      </>
    );
  }

  async function create() {
    setBusy(true);
    setError(null);
    try {
      await onCreate(name.trim() || `${source!.name} migration`, source!.id);
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  }

  return (
    <>
      <div className="topbar">
        <button className="link" onClick={() => setSource(null)}>← Choose a different source</button>
        <span className="muted">New migration · name it</span>
      </div>
      <div className="content">
        <div className="newconn">
          <h1>Name your migration</h1>
          <div className="newconn__flow">
            <ConnCard role="Source" connector={source} />
            <span className="newconn__arrow">→</span>
            <ConnCard role="Destination" connector={LAKEBASE_DESTINATION} />
          </div>
          <div className="field" style={{ maxWidth: 460 }}>
            <label>Migration name</label>
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Sales DB → Lakebase" />
          </div>
          {error && <div className="banner banner--err">{error}</div>}
          <button className="btn btn--primary btn--lg" disabled={busy} onClick={create}>
            {busy ? "Creating…" : "Create migration"}
          </button>
        </div>
      </div>
    </>
  );
}

function ConnCard({ role, connector }: { role: string; connector: Connector }) {
  return (
    <div className="conncard">
      <span className="conncard__role">{role}</span>
      <div className="conncard__body">
        <ConnectorIcon connector={connector} size={48} />
        <div>
          <div className="conncard__name">{connector.name}</div>
          <div className="conncard__cat">{connector.category}</div>
        </div>
      </div>
    </div>
  );
}
