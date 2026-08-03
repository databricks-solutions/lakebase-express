import { useEffect, useState } from "react";
import { api, type SecretPreview, type SecretRef } from "../api";
import SecretScopeField from "./SecretScopeField";

const MANUAL_KEY = "__manual_key__"; // sentinel: fall back to a free-text input

const normalizeWorkspaceHost = (host: string | null | undefined) =>
  (host ?? "").trim().replace(/^https?:\/\//i, "").replace(/\/$/, "").toLowerCase();

// Connection coordinates parsed from a secret that holds a full connection
// string. The parent form applies these to its host/database/port fields.
export interface SecretAutofill {
  host?: string | null;
  database?: string | null;
  port?: number | null;
  username?: string | null;
  sslmode?: string | null;
}

/** Password entry that accepts either a typed password or a reference to a
 *  secret. Used identically for the source and Lakebase target connections.
 *
 *  There is one "Secret" mode, not one per backend: every scope resolves through
 *  the same Databricks Secrets API, so the scope dropdown simply lists every scope
 *  the app can see regardless of cloud. On Azure workspaces that can include a Key
 *  Vault-backed scope (which reads through to the vault transparently); on AWS/GCP
 *  only Databricks-native scopes exist, and the picker works identically. The
 *  stored SecretRef.kind is always "databricks" — the backend does not branch on it.
 *
 *  In secret mode the scope and key are chosen from dropdowns populated live from
 *  the workspace, with a "type manually" escape hatch when listing is empty or a
 *  value isn't listed yet. The parent owns both `password` and `secret_ref`
 *  (mutually exclusive) so the request payload matches the backend contract. */
export default function PasswordField({
  password,
  secretRef,
  workspaceHost,
  onChange,
  onAutofill,
}: {
  password: string;
  secretRef: SecretRef | null | undefined;
  workspaceHost?: string | null;
  onChange: (next: { password: string; secret_ref: SecretRef | null }) => void;
  // Called when the selected secret holds a connection string — the parent fills
  // its host/database/port fields from the (non-secret) parsed values.
  onAutofill?: (fields: SecretAutofill) => void;
}) {
  const isSecret = !!secretRef;

  const [keys, setKeys] = useState<string[]>([]);
  const [manualKey, setManualKey] = useState(false);
  const [preview, setPreview] = useState<SecretPreview | null>(null);

  const scope = secretRef?.scope ?? "";
  const key = secretRef?.key ?? "";
  const activeWorkspace = normalizeWorkspaceHost(workspaceHost);
  const referenceWorkspace = normalizeWorkspaceHost(secretRef?.workspace_host);
  const workspaceMismatch = !!referenceWorkspace && referenceWorkspace !== activeWorkspace;

  // When a scope+key is set, peek at the secret: if it's a connection string,
  // offer to auto-fill the connection fields (the password stays server-side).
  useEffect(() => {
    setPreview(null);
    if (!isSecret || !scope || !key || workspaceMismatch) return;
    let cancelled = false;
    api.previewSecret(scope, key)
      .then((p) => {
        if (cancelled || !p.ok || !p.is_connection_string) return;
        setPreview(p);
        onAutofill?.({ host: p.host, database: p.database, port: p.port,
                       username: p.username, sslmode: p.sslmode });
      })
      .catch(() => {});
    return () => { cancelled = true; };
    // onAutofill is stable enough for this effect; keying on scope+key is what matters.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isSecret, scope, key, workspaceMismatch]);

  // Load keys whenever the chosen scope changes. Delay slightly so typing a new
  // manual scope does not issue one workspace request per keystroke.
  useEffect(() => {
    if (!isSecret || !scope || workspaceMismatch) {
      setKeys([]);
      return;
    }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      api.listSecretKeys(scope)
        .then((result) => { if (!cancelled) setKeys(result.keys); })
        .catch(() => { if (!cancelled) setKeys([]); });
    }, 200);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [isSecret, scope, workspaceMismatch]);

  const setMode = (secret: boolean) => {
    if (!secret) {
      onChange({ password, secret_ref: null });
    } else {
      onChange({
        password: "",
        secret_ref: {
          kind: "databricks",
          workspace_host: activeWorkspace || null,
          scope: secretRef?.scope ?? "",
          key: secretRef?.key ?? "",
        },
      });
    }
  };

  const setRef = (patch: Partial<SecretRef>) =>
    onChange({
      password: "",
      secret_ref: {
        kind: "databricks",
        workspace_host: activeWorkspace || secretRef?.workspace_host || null,
        scope: secretRef?.scope ?? "",
        key: secretRef?.key ?? "",
        ...patch,
      },
    });

  const keyIsListed = keys.includes(key);
  const showManualKey = manualKey || keys.length === 0 || (!!key && !keyIsListed);

  return (
    <div className="field pwdfield">
      <div className="segmented" role="tablist" aria-label="Password source">
        <button
          type="button"
          role="tab"
          aria-selected={!isSecret}
          className={`segmented__btn ${!isSecret ? "segmented__btn--on" : ""}`}
          onClick={() => setMode(false)}
        >
          Password
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={isSecret}
          className={`segmented__btn ${isSecret ? "segmented__btn--on" : ""}`}
          onClick={() => setMode(true)}
        >
          Secret
        </button>
      </div>

      {!isSecret ? (
        <input
          type="password"
          placeholder="••••••••"
          value={password}
          onChange={(e) => onChange({ password: e.target.value, secret_ref: null })}
        />
      ) : (
        <>
          <div className="field-row">
            <SecretScopeField
              value={workspaceMismatch ? "" : scope}
              onChange={(nextScope) => {
                setManualKey(false);
                setRef({ scope: nextScope, key: "" });
              }}
              newLabel="Enter manually…"
            />
            <div className="field">
              <label>Secret key</label>
              {showManualKey ? (
                <>
                  <input
                    value={key}
                    placeholder="password-key"
                    onChange={(e) => setRef({ key: e.target.value })}
                  />
                  {!!keys.length && (
                    <button
                      type="button"
                      className="link scopefield__switch"
                      onClick={() => { setManualKey(false); setRef({ key: "" }); }}
                    >
                      Choose an existing key
                    </button>
                  )}
                </>
              ) : (
                <select
                  value={keyIsListed ? key : ""}
                  onChange={(e) => {
                    if (e.target.value === MANUAL_KEY) { setManualKey(true); return; }
                    setRef({ key: e.target.value });
                  }}
                >
                  <option value="" disabled>Select a key…</option>
                  {keys.map((k) => (
                    <option key={k} value={k}>{k}</option>
                  ))}
                  <option value={MANUAL_KEY}>Enter manually…</option>
                </select>
              )}
            </div>
          </div>
          {workspaceMismatch && (
            <p className="pwdfield__hint pwdfield__hint--err">
              This reference belongs to {referenceWorkspace}, but the active workspace is
              {activeWorkspace ? ` ${activeWorkspace}` : " unavailable"}. Switch back or choose
              a scope in the active workspace.
            </p>
          )}
          {preview?.is_connection_string && (
            <p className="pwdfield__hint pwdfield__hint--ok">
              Connection string detected — filled the host{preview.database ? ", database" : ""}
              {preview.port ? ", port" : ""} above from it. The password is read at connect time.
            </p>
          )}
          <p className="muted pwdfield__hint">
            Reads the password from this secret at connect time — any Databricks
            secret scope, including cloud-backed scopes such as Azure Key Vault where
            available. If the secret holds a full connection string, the
            host/database/port are filled in for you. Projects persist only this
            reference; Async Mode can explicitly copy the resolved password into its
            selected runtime scope for a generated job.
          </p>
        </>
      )}
    </div>
  );
}
