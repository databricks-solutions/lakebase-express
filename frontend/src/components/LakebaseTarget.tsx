import { useState } from "react";
import { api, type LakebaseConn, type SecretRef } from "../api";
import type { MigrationState } from "../App";
import PasswordField from "./PasswordField";

const DEFAULT: LakebaseConn = {
  host: "",
  database: "databricks_postgres",
  user: "",
  password: "",
  port: 5432,
  sslmode: "require",
};

/** Lakebase target connection — controlled by the shared migration state so the
 *  Schema and Data phases reuse a single configured target. */
export default function LakebaseTarget({
  state,
  setState,
  onSave,
  workspaceHost,
}: {
  state: MigrationState;
  setState: React.Dispatch<React.SetStateAction<MigrationState>>;
  onSave?: () => Promise<void>;
  workspaceHost?: string | null;
}) {
  const conn = state.lakebase ?? DEFAULT;
  const [msg, setMsg] = useState<string | null>(null);
  const [ok, setOk] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);

  const set = (k: keyof LakebaseConn, v: string | number) => {
    setSaved(false);
    setState((s) => ({ ...s, lakebase: { ...(s.lakebase ?? DEFAULT), [k]: v } }));
  };

  const setPassword = (next: { password: string; secret_ref: SecretRef | null }) => {
    setSaved(false);
    setState((s) => ({ ...s, lakebase: { ...(s.lakebase ?? DEFAULT), ...next } }));
  };

  const setPreserveCase = (preserve: boolean) => {
    setSaved(false);
    setState((s) => ({
      ...s,
      identifierCase: preserve ? "preserve" : "lowercase",
      // Names are embedded throughout both artifacts. Force regeneration so a
      // project cannot apply/validate a plan built with the previous policy.
      plan: null,
      validation: null,
    }));
  };

  // A secret holding a full connection string fills the target coordinates too.
  // Only apply fields the string carried (note: the target's role field is `user`).
  const autofill = (f: { host?: string | null; database?: string | null; port?: number | null; username?: string | null; sslmode?: string | null }) => {
    setSaved(false);
    setState((s) => {
      const c = s.lakebase ?? DEFAULT;
      return { ...s, lakebase: {
        ...c,
        host: f.host ?? c.host,
        database: f.database ?? c.database,
        port: f.port ?? c.port,
        user: f.username ?? c.user,
        sslmode: f.sslmode ?? c.sslmode,
      } };
    });
  };

  async function save() {
    if (!onSave) return;
    await onSave();
    setSaved(true);
    setTimeout(() => setSaved(false), 2500);
  }

  async function test() {
    setBusy(true);
    setMsg(null);
    try {
      const r = await api.testLakebase(conn);
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
    <section className="card">
      <h2>Target: Databricks Lakebase</h2>
      <p className="muted">
        The app connects here to apply schema/code and load data. Use a Postgres role with
        create/insert privileges on the target database.
      </p>

      <div className="field-row">
        <div className="field">
          <label>Host</label>
          <input
            value={conn.host}
            placeholder="instance-name.database.cloud.databricks.com"
            onChange={(e) => set("host", e.target.value)}
          />
        </div>
        <div className="field field--narrow">
          <label>Port</label>
          <input type="number" value={conn.port} onChange={(e) => set("port", Number(e.target.value))} />
        </div>
      </div>
      <div className="field-row">
        <div className="field">
          <label>Database</label>
          <input value={conn.database} onChange={(e) => set("database", e.target.value)} />
        </div>
        <div className="field field--narrow">
          <label>SSL mode</label>
          <input value={conn.sslmode} onChange={(e) => set("sslmode", e.target.value)} />
        </div>
      </div>
      <div className="field">
        <label>User (role)</label>
        <input value={conn.user} onChange={(e) => set("user", e.target.value)} />
      </div>
      <div className="field">
        <label>Identifier casing</label>
        <label className="check">
          <input
            type="checkbox"
            checked={state.identifierCase === "preserve"}
            onChange={(e) => setPreserveCase(e.target.checked)}
          />
          Preserve source schema and object casing
        </label>
        <p className="muted">
          Off by default: names are lower-cased for PostgreSQL. Turn this on when existing
          applications require exact source names such as <code>SalesLT.Product</code>;
          those names are case-sensitive and must be double-quoted in PostgreSQL queries.
        </p>
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
          {busy ? "Testing…" : "Test Lakebase connection"}
        </button>
        {onSave && <button className="btn btn--primary" onClick={save}>Save</button>}
        {saved && <span className="saved-flag">✓ Saved</span>}
      </div>
      {msg && <div className={`banner ${ok ? "banner--ok" : "banner--err"}`}>{msg}</div>}
    </section>
  );
}
