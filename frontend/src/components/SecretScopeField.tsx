import { useEffect, useState } from "react";
import { api, type SecretScopeOption } from "../api";

const NEW_SCOPE = "__new_scope__";

/** Reusable Databricks secret-scope picker.
 *
 * Existing visible scopes use a select. If listing is unavailable, the current
 * value does not exist yet, or the user chooses "Create / enter a new scope",
 * it falls back to the same free-text field used before scope discovery existed.
 */
export default function SecretScopeField({
  value,
  onChange,
  onResolvedOption,
  label = "Secret scope",
  newLabel = "Create / enter a new scope…",
}: {
  value: string;
  onChange: (scope: string) => void;
  onResolvedOption?: (scope: SecretScopeOption | null) => void;
  label?: string;
  newLabel?: string;
}) {
  const [scopes, setScopes] = useState<SecretScopeOption[] | null>(null);
  const [manual, setManual] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api.listSecretScopes()
      .then((result) => { if (!cancelled) setScopes(result.scopes); })
      .catch(() => { if (!cancelled) setScopes([]); });
    return () => { cancelled = true; };
  }, []);

  const listed = !!scopes?.some((scope) => scope.name === value);
  const resolvedOption = scopes?.find((scope) => scope.name === value) ?? null;
  const showManual = manual || (scopes !== null && (scopes.length === 0 || (!!value && !listed)));

  useEffect(() => {
    onResolvedOption?.(resolvedOption);
  }, [onResolvedOption, resolvedOption]);

  return (
    <div className="field">
      <label>{label}</label>
      {showManual ? (
        <>
          <input
            value={value}
            placeholder="my-scope"
            onChange={(event) => onChange(event.target.value)}
          />
          {!!scopes?.length && (
            <button
              type="button"
              className="link scopefield__switch"
              onClick={() => {
                setManual(false);
                if (!listed) onChange("");
              }}
            >
              Choose an existing scope
            </button>
          )}
        </>
      ) : scopes === null ? (
        <input value={value} placeholder="Loading scopes…" readOnly />
      ) : (
        <select
          value={listed ? value : ""}
          onChange={(event) => {
            if (event.target.value === NEW_SCOPE) {
              setManual(true);
              onChange("");
              return;
            }
            onChange(event.target.value);
          }}
        >
          <option value="" disabled>Select a scope…</option>
          {scopes.map((scope) => (
            <option key={scope.name} value={scope.name}>
              {scope.name}{scope.backend_type === "AZURE_KEYVAULT" ? " (Key Vault)" : ""}
            </option>
          ))}
          <option value={NEW_SCOPE}>{newLabel}</option>
        </select>
      )}
    </div>
  );
}
