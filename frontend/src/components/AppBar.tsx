import type { ReactNode } from "react";
import type { WorkspaceStatus } from "../api";

/** Global Databricks-style top bar: brand on the left, a context slot in the
 *  center (global search on the home shell, breadcrumb inside a project), and
 *  workspace actions on the right. */
export default function AppBar({
  children,
  workspace,
  onManageWorkspace,
  onHome,
}: {
  children?: ReactNode;
  workspace?: WorkspaceStatus | null;
  onManageWorkspace?: () => void;
  onHome?: () => void;
}) {
  return (
    <header className="appbar">
      <button
        type="button"
        className="appbar__brand"
        onClick={onHome}
        aria-label="Lakebase Express — go to migrations home"
        title="Migrations home"
      >
        <span className="appbar__logo"><img src="/lakebase-icon.png" alt="Lakebase" /></span>
        <span className="appbar__word">Lakebase&nbsp;Express</span>
      </button>
      <div className="appbar__center">{children}</div>
      <div className="appbar__actions">
        <WorkspaceChip workspace={workspace} onManage={onManageWorkspace} />
        <button className="iconbtn-bar" title="Help" aria-label="Help">
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2">
            <circle cx="12" cy="12" r="9" />
            <path d="M9.5 9a2.5 2.5 0 1 1 3.5 2.3c-.8.4-1 .9-1 1.7" />
            <circle cx="12" cy="17" r="0.6" fill="currentColor" stroke="none" />
          </svg>
        </button>
        <span className="avatar" title="Workspace user">DB</span>
      </div>
    </header>
  );
}

/** The workspace "name": the first DNS label of the host (e.g.
 *  adb-1234567890.7.azuredatabricks.net → adb-1234567890). */
function workspaceName(host?: string): string {
  if (!host) return "";
  const h = host.replace(/^https?:\/\//, "").replace(/\/+$/, "");
  return h.split(".")[0] || h;
}

/** Compact indicator for the bound workspace. Opens Settings for the details;
 *  the workspace itself is fixed at startup and not changeable from the UI. */
function WorkspaceChip({ workspace, onManage }: { workspace?: WorkspaceStatus | null; onManage?: () => void }) {
  if (!workspace) return null;
  if (!workspace.connected) {
    return (
      <button className="wschip wschip--off" onClick={onManage} title="No Databricks workspace configured">
        <span className="wschip__dot" /> No workspace
      </button>
    );
  }
  return (
    <button
      className="wschip"
      onClick={onManage}
      title={`${workspace.host ?? ""}${workspace.user ? ` · ${workspace.user}` : ""}`}
    >
      <span className="wschip__dot wschip__dot--on" />
      <span className="wschip__host">{workspaceName(workspace.host)}</span>
    </button>
  );
}

/** Global search input rendered in the app bar on the migrations home. */
export function GlobalSearch({ value, onChange, placeholder }: { value: string; onChange: (v: string) => void; placeholder: string }) {
  return (
    <div className="appsearch">
      <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2">
        <circle cx="11" cy="11" r="7" />
        <path d="m20 20-3.5-3.5" />
      </svg>
      <input value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} />
    </div>
  );
}
