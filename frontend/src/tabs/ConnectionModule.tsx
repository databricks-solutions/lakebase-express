import { useState } from "react";
import { api, type ConnectionRequest, type SecretRef } from "../api";
import type { Connector } from "../connectors";
import type { MigrationState } from "../App";
import LakebaseTarget from "../components/LakebaseTarget";
import PasswordField from "../components/PasswordField";

interface Props {
  source: Connector;
  state: MigrationState;
  setState: React.Dispatch<React.SetStateAction<MigrationState>>;
  onSave: () => Promise<void>;
  workspaceHost?: string | null;
}

/** Configure and test BOTH connections — source and the Lakebase target. This is
 *  the single place connections live; other modules reuse them from project state. */
export default function ConnectionModule({ source, state, setState, onSave, workspaceHost }: Props) {
  const conn = state.connection as ConnectionRequest;
  const [msg, setMsg] = useState<string | null>(null);
  const [ok, setOk] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);

  const set = (k: keyof ConnectionRequest, v: string | number) => {
    setSaved(false);
    setState((s) => ({ ...s, connection: { ...(s.connection as ConnectionRequest), [k]: v } }));
  };

  const setPassword = (next: { password: string; secret_ref: SecretRef | null }) => {
    setSaved(false);
    setState((s) => ({ ...s, connection: { ...(s.connection as ConnectionRequest), ...next } }));
  };

  // A secret that holds a full connection string fills the coordinates too. Only
  // apply fields the string actually carried (don't wipe existing values).
  const autofill = (f: { host?: string | null; database?: string | null; port?: number | null; username?: string | null }) => {
    setSaved(false);
    setState((s) => {
      const c = s.connection as ConnectionRequest;
      return { ...s, connection: {
        ...c,
        host: f.host ?? c.host,
        database: f.database ?? c.database,
        port: f.port ?? c.port,
        username: f.username ?? c.username,
      } };
    });
  };

  async function save() {
    await onSave();
    setSaved(true);
    setTimeout(() => setSaved(false), 2500);
  }

  async function test() {
    setBusy(true);
    setMsg(null);
    try {
      const r = await api.testConnection(conn);
      setOk(r.ok);
      setMsg(r.message);
    } catch (e) {
      setOk(false);
      setMsg((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack">
      <div className="grid conn-grid">
      <section className="card">
        <h2>Source · {source.name}</h2>
        <p className="muted">
          Password values are not saved in the project. Secret mode saves only its
          workspace/scope/key reference; successfully tested typed passwords may be retained
          by the encrypted credential store so a migration can resume.
        </p>
        {source.connectionNote && <div className="banner banner--ok">{source.connectionNote}</div>}

        <div className="field" style={{ marginTop: 14 }}>
          <label>Server host</label>
          <input value={conn.host} placeholder={source.hostPlaceholder ?? "host"} onChange={(e) => set("host", e.target.value)} />
        </div>
        <div className="field-row">
          <div className="field">
            <label>Database</label>
            <input value={conn.database} onChange={(e) => set("database", e.target.value)} />
          </div>
          <div className="field field--narrow">
            <label>Port</label>
            <input type="number" value={conn.port} onChange={(e) => set("port", Number(e.target.value))} />
          </div>
        </div>
        <div className="field">
          <label>Username</label>
          <input value={conn.username} placeholder={source.usernamePlaceholder ?? "username"} onChange={(e) => set("username", e.target.value)} />
        </div>
        <PasswordField
          password={conn.password}
          secretRef={conn.secret_ref}
          workspaceHost={workspaceHost}
          onChange={setPassword}
          onAutofill={autofill}
        />

        <div className="actions">
          <button className="btn" disabled={busy} onClick={test}>
            {busy ? "Testing…" : "Test source connection"}
          </button>
        </div>
        {msg && <div className={`banner ${ok ? "banner--ok" : "banner--err"}`}>{msg}</div>}
      </section>

      <div className="migrate-arrow" role="img" aria-label="migrates to" title="Migrate to Lakebase">
        <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor"
             strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <line x1="4" y1="12" x2="19" y2="12" />
          <polyline points="13 6 19 12 13 18" />
        </svg>
      </div>

      <LakebaseTarget state={state} setState={setState} workspaceHost={workspaceHost} />
      </div>

      <div className="card savebar">
        <div>
          <strong>Save connection</strong>
          <p className="muted">
            Saves source/target coordinates and non-secret secret references to this project.
            Password values are never included in the project.
          </p>
        </div>
        <div className="savebar__actions">
          {saved && <span className="saved-flag">✓ Saved</span>}
          <button className="btn btn--primary" onClick={save}>Save connection</button>
        </div>
      </div>
    </div>
  );
}
