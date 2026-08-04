import { useEffect, useState } from "react";
import { api, type FmEndpoint, type WorkspaceStatus } from "../api";

interface Props {
  fmEndpoint: string;
  setFmEndpoint: (v: string) => void;
  workspace: WorkspaceStatus | null;
}

export default function Settings({ fmEndpoint, setFmEndpoint, workspace }: Props) {
  const [endpoints, setEndpoints] = useState<FmEndpoint[]>([]);
  const [defaultEp, setDefaultEp] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

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
            The workspace used for the AI assessment &amp; code translation (Foundation Model),
            endpoint listing, and creating Databricks Jobs. Each migration targets a single
            workspace, fixed when the app starts — it cannot be changed from here.
          </p>

          {workspace?.connected ? (
            <div className="wsstatus">
              <span className="dot dot--ok" />
              <span>
                Connected to <strong>{workspace.host}</strong>
                {workspace.user ? <> as {workspace.user}</> : null}
              </span>
            </div>
          ) : (
            <>
              <div className="wsstatus">
                <span className="dot" />
                <span>Not connected to a Databricks workspace.</span>
              </div>
              <p className="muted">
                Running locally, start the backend with a CLI profile:{" "}
                <code>DATABRICKS_CONFIG_PROFILE=&lt;your-profile&gt;</code>. Deployed as a
                Databricks App, the App's own identity is used automatically.
              </p>
              {workspace?.error && <div className="banner banner--err">{workspace.error}</div>}
            </>
          )}
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
