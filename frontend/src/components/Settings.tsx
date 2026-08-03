import { useEffect, useState } from "react";
import { api, type FmEndpoint, type WorkspaceStatus } from "../api";

interface Props {
  fmEndpoint: string;
  setFmEndpoint: (v: string) => void;
  workspace: WorkspaceStatus | null;
  onLogin: (host: string) => Promise<void>;
  onLogout: () => void;
}

export default function Settings({ fmEndpoint, setFmEndpoint, workspace, onLogin, onLogout }: Props) {
  const [endpoints, setEndpoints] = useState<FmEndpoint[]>([]);
  const [defaultEp, setDefaultEp] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const [host, setHost] = useState("");
  const [wsBusy, setWsBusy] = useState(false);
  const [wsError, setWsError] = useState<string | null>(null);

  async function login() {
    if (!host.trim()) return;
    setWsBusy(true);
    setWsError(null);
    try {
      await onLogin(host.trim());
    } catch (e) {
      setWsError((e as Error).message);
    } finally {
      setWsBusy(false);
    }
  }

  useEffect(() => {
    api
      .listFmEndpoints()
      .then((r) => {
        setEndpoints(r.endpoints);
        setDefaultEp(r.default);
        if (r.error) setError(r.error);
        // Adopt the workspace default until the user picks one.
        if (!fmEndpoint) setFmEndpoint(r.default);
      })
      .catch((e) => setError((e as Error).message))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Settings</h1>
          <p className="muted">Workspace-level configuration for the accelerator.</p>
        </div>
      </div>

      <div className="content">
        <div className="card">
          <h2>Databricks workspace</h2>
          <p className="muted">
            Sign in to the Databricks workspace used for the AI assessment &amp; code translation
            (Foundation Model), endpoint listing, and creating Databricks Jobs.
          </p>

          {workspace?.connected ? (
            <div className="wsstatus">
              <span className="dot dot--ok" />
              <span>
                Connected to <strong>{workspace.host}</strong>
                {workspace.user ? <> as {workspace.user}</> : null}{" "}
                <span className="badge badge--soon">{workspace.source === "oauth" ? "OAuth" : "Ambient identity"}</span>
              </span>
              {workspace.source === "oauth" && (
                <button className="btn btn--sm" onClick={onLogout} style={{ marginLeft: "auto" }}>Log out</button>
              )}
            </div>
          ) : (
            <p className="muted">Not connected — sign in below.</p>
          )}

          <div className="field" style={{ maxWidth: 460, marginTop: 14 }}>
            <label>Workspace URL</label>
            <input
              value={host}
              placeholder="https://adb-1234567890.11.azuredatabricks.net"
              onChange={(e) => setHost(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && login()}
            />
          </div>
          <div className="actions">
            <button className="btn btn--primary" disabled={wsBusy || !host.trim()} onClick={login}>
              {wsBusy ? "Opening Databricks…" : workspace?.source === "oauth" ? "Switch workspace" : "Log in with Databricks"}
            </button>
          </div>
          <p className="muted">A Databricks sign-in window opens; approve access, then it closes automatically.</p>
          {wsError && <div className="banner banner--err">{wsError}</div>}
        </div>

        <div className="card" style={{ marginTop: 24 }}>
          <h2>Foundation Model endpoint</h2>
          <p className="muted">
            Used by the Schema &amp; Code phase to translate T-SQL into Postgres / PL-pgSQL. The
            list shows chat-capable serving endpoints in this workspace.
          </p>

          <div className="field" style={{ maxWidth: 460, marginTop: 12 }}>
            <label>Translation endpoint</label>
            <select
              value={fmEndpoint}
              onChange={(e) => setFmEndpoint(e.target.value)}
              disabled={loading}
            >
              {/* Always offer the configured default, even if listing failed. */}
              {defaultEp && !endpoints.some((e) => e.name === defaultEp) && (
                <option value={defaultEp}>{defaultEp} (default)</option>
              )}
              {endpoints.map((e) => (
                <option key={e.name} value={e.name} disabled={!e.ready}>
                  {e.name}
                  {e.name === defaultEp ? " (default)" : ""}
                  {e.ready ? "" : " — not ready"}
                </option>
              ))}
            </select>
          </div>

          {loading && <p className="muted">Loading endpoints…</p>}
          {error && (
            <div className="banner banner--err">
              Could not list serving endpoints ({error}). The configured default is still used.
            </div>
          )}
          <p className="muted">
            Override the default via the <code>LBX_FM_ENDPOINT</code> app environment variable.
          </p>
        </div>

        <div className="card" style={{ marginTop: 24 }}>
          <h2>Sizing assumptions</h2>
          <p className="muted">
            Edit <code>backend/sizing/pricing.yaml</code> to tune CU prices, storage rates, and the
            Azure → Lakebase conversion ratios.
          </p>
        </div>
      </div>
    </>
  );
}
