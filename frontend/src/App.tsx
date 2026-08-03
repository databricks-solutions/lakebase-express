import { useCallback, useEffect, useRef, useState } from "react";
import { api, type AssessmentReport, type ConnectionRequest, type IdentifierCase, type LakebaseConn, type PhaseStatus, type PlanItem, type Project, type QueryParityReport, type SecretRef, type ValidationReport, type WorkspaceStatus } from "./api";
import { SOURCE_CONNECTORS, type Connector } from "./connectors";
import Sidebar, { type NavId } from "./components/Sidebar";
import AppBar, { GlobalSearch } from "./components/AppBar";
import MigrationsHome from "./components/MigrationsHome";
import NewMigration from "./components/NewMigration";
import Settings from "./components/Settings";
import ProjectSidebar, { MODULES, type ModuleId } from "./components/ProjectSidebar";
import Overview from "./components/Overview";
import ConnectionModule from "./tabs/ConnectionModule";
import AssessmentModule from "./tabs/AssessmentModule";
import SchemaCode from "./tabs/SchemaCode";
import DataMigration from "./tabs/DataMigration";
import CreateSync from "./tabs/CreateSync";
import ValidationModule from "./tabs/ValidationModule";
import QueryParityModule from "./tabs/QueryParityModule";

// What the phase components consume. Derived from the persisted Project plus the
// session-only secrets (passwords are never persisted).
export interface MigrationState {
  connection: ConnectionRequest | null;
  report: AssessmentReport | null;
  lakebase: LakebaseConn | null;
  targetSchema: string;
  identifierCase: IdentifierCase;
  plan: PlanItem[] | null;
  // Data Migration marks the tables + load options; concluded in Create Sync.
  selection: string[];
  dataOptions: { truncate_first: boolean; batch_size: number };
  // Post-migration validation report (independent module).
  validation: ValidationReport | null;
  // Post-migration query-parity report (independent module).
  queryParity: QueryParityReport | null;
}

interface Secrets {
  source: string;
  target: string;
  // References are persisted on the project, while these fields overlay them
  // during edits so changes flow through the derived connection objects before
  // the debounced project save completes.
  sourceRef: SecretRef | null;
  targetRef: SecretRef | null;
}

export default function App() {
  const [view, setView] = useState<NavId>("migrations");
  const [creating, setCreating] = useState(false);
  const [project, setProject] = useState<Project | null>(null);
  const [fmEndpoint, setFmEndpoint] = useState("");
  const [query, setQuery] = useState("");
  const [ws, setWs] = useState<WorkspaceStatus | null>(null);

  const refreshWs = useCallback(() => { api.dbStatus().then(setWs).catch(() => {}); }, []);
  useEffect(() => { refreshWs(); }, [refreshWs]);

  // Open the workspace OAuth flow in a popup; refresh status when it completes.
  const loginWs = useCallback(async (host: string) => {
    const { auth_url } = await api.dbOauthStart(host);
    const popup = window.open(auth_url, "databricks-oauth", "width=600,height=760");
    const onMsg = (e: MessageEvent) => {
      if (e.data === "lbx-databricks-auth") { cleanup(); refreshWs(); }
    };
    const timer = window.setInterval(() => { if (popup?.closed) { cleanup(); refreshWs(); } }, 800);
    function cleanup() { window.removeEventListener("message", onMsg); window.clearInterval(timer); }
    window.addEventListener("message", onMsg);
  }, [refreshWs]);

  const logoutWs = useCallback(async () => { setWs(await api.dbLogout()); }, []);

  // Passwords live only in memory for the session.
  const secretsRef = useRef<Secrets>({ source: "", target: "", sourceRef: null, targetRef: null });
  const [, forceRender] = useState(0);

  function navigate(v: NavId) {
    setProject(null);
    setCreating(false);
    setView(v);
  }

  async function openProject(id: string) {
    const p = await api.getProject(id);
    secretsRef.current = { source: "", target: "", sourceRef: null, targetRef: null };
    setProject(p);
    setCreating(false);
  }

  async function createProject(name: string, connectorId: string) {
    const p = await api.createProject(name, connectorId);
    secretsRef.current = { source: "", target: "", sourceRef: null, targetRef: null };
    setProject(p);
    setCreating(false);
  }

  // A project opens its own shell (module rail). Otherwise the app-level shell.
  if (project) {
    return (
      <ProjectWorkspace
        project={project}
        setProject={setProject}
        secretsRef={secretsRef}
        forceRender={() => forceRender((x) => x + 1)}
        fmEndpoint={fmEndpoint}
        workspace={ws}
        onManageWorkspace={() => navigate("settings")}
        onExit={() => navigate("migrations")}
      />
    );
  }

  return (
    <div className="app">
      <AppBar workspace={ws} onManageWorkspace={() => navigate("settings")} onHome={() => navigate("migrations")}>
        {!creating && view === "migrations" && (
          <GlobalSearch value={query} onChange={setQuery} placeholder="Search migrations, sources, and settings" />
        )}
      </AppBar>
      <div className="shell">
        <Sidebar active={view} onNavigate={navigate} onNew={() => setCreating(true)} />
        <div className="main">
          {creating ? (
            <NewMigration onCancel={() => setCreating(false)} onCreate={createProject} />
          ) : view === "migrations" ? (
            <MigrationsHome query={query} onNew={() => setCreating(true)} onOpen={openProject} />
          ) : (
            <Settings
              fmEndpoint={fmEndpoint}
              setFmEndpoint={setFmEndpoint}
              workspace={ws}
              onLogin={loginWs}
              onLogout={logoutWs}
            />
          )}
        </div>
      </div>
    </div>
  );
}

// --- Workspace (dashboard + phase views) -----------------------------------------

function deriveState(p: Project, s: Secrets): MigrationState {
  return {
    connection: {
      source_type: p.source.source_type,
      host: p.source.host,
      database: p.source.database,
      username: p.source.username,
      port: p.source.port,
      password: s.source,
      // The password pointer is persisted on the project (non-secret), so a
      // resumed session restores Secret mode. Session state overlays it only
      // while the user is actively editing before the next save.
      secret_ref: s.sourceRef ?? p.source.secret_ref ?? null,
      project_id: p.id,
    },
    report: p.assessment,
    lakebase: {
      host: p.target.host,
      database: p.target.database,
      user: p.target.user,
      port: p.target.port,
      sslmode: p.target.sslmode,
      password: s.target,
      secret_ref: s.targetRef ?? p.target.secret_ref ?? null,
      project_id: p.id,
    },
    targetSchema: p.target_schema,
    identifierCase: p.identifier_case ?? "lowercase",
    plan: p.plan,
    selection: p.selection ?? [],
    dataOptions: {
      truncate_first: p.data_options?.truncate_first ?? true,
      batch_size: p.data_options?.batch_size ?? 5000,
    },
    validation: p.validation,
    queryParity: p.query_parity,
  };
}

function computeStatuses(p: Project): Record<string, PhaseStatus> {
  return {
    assessment: p.assessment ? "done" : "not_started",
    sizing: p.assessment ? "in_progress" : "not_started",
    schema: p.plan ? "in_progress" : "not_started",
    data: p.statuses?.data ?? "not_started",
    validation: p.validation ? "done" : "not_started",
    parity: p.query_parity ? "done" : "not_started",
  };
}

interface WorkspaceProps {
  project: Project;
  setProject: React.Dispatch<React.SetStateAction<Project | null>>;
  secretsRef: React.MutableRefObject<Secrets>;
  forceRender: () => void;
  fmEndpoint: string;
  workspace: WorkspaceStatus | null;
  onManageWorkspace: () => void;
  onExit: () => void;
}

function ProjectWorkspace({ project, setProject, secretsRef, forceRender, fmEndpoint, workspace, onManageWorkspace, onExit }: WorkspaceProps) {
  const [module, setModule] = useState<ModuleId>("overview");
  const lastSaved = useRef<string>("");
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Seed the save baseline when a project is first shown so we don't re-PUT it.
  useEffect(() => {
    lastSaved.current = JSON.stringify({ ...project, statuses: computeStatuses(project) });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project.id]);

  // Debounced autosave whenever the persisted project content changes.
  useEffect(() => {
    const payload = { ...project, statuses: computeStatuses(project) };
    const serialized = JSON.stringify(payload);
    if (serialized === lastSaved.current) return;
    if (saveTimer.current) clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(() => {
      api.updateProject(project.id, payload).then((saved) => {
        lastSaved.current = JSON.stringify(saved);
      }).catch(() => {});
    }, 800);
    return () => {
      if (saveTimer.current) clearTimeout(saveTimer.current);
    };
  }, [project]);

  const state = deriveState(project, secretsRef.current);

  const setState = useCallback<React.Dispatch<React.SetStateAction<MigrationState>>>(
    (updater) => {
      setProject((prev) => {
        if (!prev) return prev;
        const current = deriveState(prev, secretsRef.current);
        const next = typeof updater === "function" ? (updater as (s: MigrationState) => MigrationState)(current) : updater;
        secretsRef.current = {
          source: next.connection?.password ?? "",
          target: next.lakebase?.password ?? "",
          sourceRef: next.connection?.secret_ref ?? null,
          targetRef: next.lakebase?.secret_ref ?? null,
        };
        return {
          ...prev,
          source: next.connection
            ? { source_type: next.connection.source_type, host: next.connection.host, database: next.connection.database, username: next.connection.username, port: next.connection.port, secret_ref: next.connection.secret_ref ?? null }
            : prev.source,
          target: next.lakebase
            ? { host: next.lakebase.host, database: next.lakebase.database, user: next.lakebase.user, port: next.lakebase.port, sslmode: next.lakebase.sslmode, secret_ref: next.lakebase.secret_ref ?? null }
            : prev.target,
          target_schema: next.targetSchema,
          identifier_case: next.identifierCase,
          assessment: next.report ?? prev.assessment,
          plan: next.plan,
          selection: next.selection,
          data_options: next.dataOptions,
          validation: next.validation,
          query_parity: next.queryParity,
        };
      });
      forceRender(); // secretsRef mutation needs a render to flow into derived state
    },
    [setProject, secretsRef, forceRender],
  );

  // Immediate, explicit save (flushes the debounced autosave) for the Save button.
  const saveNow = useCallback(async () => {
    const payload = { ...project, statuses: computeStatuses(project) };
    const saved = await api.updateProject(project.id, payload);
    lastSaved.current = JSON.stringify(saved);
  }, [project]);

  const sourceConnector: Connector =
    SOURCE_CONNECTORS.find((c) => c.id === project.source_connector_id) ?? SOURCE_CONNECTORS[0];

  const done: Record<ModuleId, boolean> = {
    overview: false,
    connection: !!(state.connection?.host && state.lakebase?.host),
    assessment: !!state.report,
    schema: !!state.plan,
    data: state.selection.length > 0,
    sync: false,
    validation: !!state.validation,
    parity: !!state.queryParity,
  };
  const meta = MODULES.find((m) => m.id === module)!;
  const goConnection = () => setModule("connection");

  // Previous/Next step navigation across the ordered journey.
  const idx = MODULES.findIndex((m) => m.id === module);
  const prevM = idx > 0 ? MODULES[idx - 1] : null;
  const nextM = idx < MODULES.length - 1 ? MODULES[idx + 1] : null;

  return (
    <div className="app">
      <AppBar workspace={workspace} onManageWorkspace={onManageWorkspace} onHome={onExit}>
        <nav className="crumbs">
          <button className="crumbs__link" onClick={onExit}>Migrations</button>
          <span className="crumbs__sep">/</span>
          <span className="crumbs__current">{project.name}</span>
        </nav>
      </AppBar>
      <div className="shell">
        <ProjectSidebar
          projectName={project.name}
          source={sourceConnector}
          active={module}
          onSelect={setModule}
          onBack={onExit}
          done={done}
        />
        <div className="main">
          <div className="page-head">
            <div>
              <h1>{meta.label}</h1>
              <p className="muted">{meta.desc}</p>
            </div>
          </div>
        <div className="content">
          {module === "overview" && <Overview source={sourceConnector} done={done} onOpen={setModule} />}
          {module === "connection" && (
            <ConnectionModule
              source={sourceConnector}
              state={state}
              setState={setState}
              onSave={saveNow}
              workspaceHost={workspace?.host}
            />
          )}
          {module === "assessment" && <AssessmentModule state={state} setState={setState} goConnection={goConnection} fmEndpoint={fmEndpoint} />}
          {module === "schema" && <SchemaCode state={state} setState={setState} fmEndpoint={fmEndpoint} />}
          {module === "data" && <DataMigration state={state} setState={setState} onGoConnection={goConnection} onContinue={() => setModule("sync")} />}
          {module === "sync" && <CreateSync state={state} onGoConnection={goConnection} onGoSchema={() => setModule("schema")} onGoData={() => setModule("data")} onGoValidation={() => setModule("validation")} workspace={workspace} onManageWorkspace={onManageWorkspace} />}
          {module === "validation" && <ValidationModule state={state} setState={setState} goConnection={goConnection} fmEndpoint={fmEndpoint} />}
          {module === "parity" && <QueryParityModule state={state} setState={setState} goConnection={goConnection} goAssessment={() => setModule("assessment")} fmEndpoint={fmEndpoint} />}

          {module !== "overview" && (
            <div className="stepnav">
              {prevM ? (
                <button className="btn" onClick={() => setModule(prevM.id)}>← {prevM.label}</button>
              ) : <span />}
              {nextM ? (
                <button className="btn btn--primary" onClick={() => setModule(nextM.id)}>{nextM.label} →</button>
              ) : <span />}
            </div>
          )}
          </div>
        </div>
      </div>
    </div>
  );
}
