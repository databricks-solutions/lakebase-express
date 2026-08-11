// Typed API client + shared types. Field names mirror the backend Pydantic models
// in backend/assessment/models.py — keep the two in sync.

// A pointer to a password held in a secret manager instead of typed inline.
// Resolution is cloud-agnostic — every scope is read by name through the
// Databricks Secrets API. On Azure a scope may be Key Vault-backed (then `key`
// is the Key Vault secret name); on AWS/GCP only Databricks-native scopes exist.
// `kind` is a UI label only. Mirrors backend SecretRef.
export type SecretKind = "databricks" | "azure_key_vault";

export interface SecretRef {
  kind: SecretKind;
  // Scope/key names are workspace-local. New references bind to the active host
  // so a later workspace switch cannot silently resolve the same names elsewhere.
  workspace_host?: string | null;
  scope: string;
  key: string;
}

export interface SecretScopeOption {
  name: string;
  backend_type: string;
}

// Non-secret fields parsed from a secret that holds a full connection string
// (password intentionally omitted — it never leaves the backend).
export interface SecretPreview {
  ok: boolean;
  is_connection_string: boolean;
  host?: string | null;
  database?: string | null;
  port?: number | null;
  username?: string | null;
  sslmode?: string | null;
}

export interface ConnectionRequest {
  source_type: string;
  host: string;
  database: string;
  username: string;
  password: string;
  // Alternative to a typed password: resolve it from a secret manager.
  secret_ref?: SecretRef | null;
  port: number;
  // Scopes a remembered/resolved password to this migration project.
  project_id?: string;
}

export interface ConnectionResult {
  ok: boolean;
  message: string;
}

export type Severity = "info" | "low" | "medium" | "high";

export interface Finding {
  rule_id: string;
  title: string;
  severity: Severity;
  object_name: string;
  detail: string;
  recommendation: string;
}

export interface ColumnInfo {
  name: string;
  data_type: string;
  max_length?: number | null;
  precision?: number | null;
  scale?: number | null;
  is_nullable: boolean;
}

export interface ForeignKeyInfo {
  name: string;
  columns: string[];
  ref_schema: string;
  ref_table: string;
  ref_columns: string[];
  on_delete: string;
  on_update: string;
}

export interface IndexColumnInfo {
  name: string;
  descending: boolean;
}

export interface IndexInfo {
  name: string;
  columns: IndexColumnInfo[];
  include_columns: string[];
  is_unique: boolean;
  filter_definition?: string | null;
}

export interface ColumnDefaultInfo {
  column: string;
  definition: string;
}

export interface CheckConstraintInfo {
  name: string;
  definition: string;
}

export interface TableInfo {
  schema_name: string;
  table_name: string;
  row_count: number;
  column_count: number;
  columns: ColumnInfo[];
  primary_key: string[];
  // Constraint/index metadata for the plan's post-data phase (older saved
  // reports may not carry these yet).
  identity_column?: string | null;
  foreign_keys?: ForeignKeyInfo[];
  indexes?: IndexInfo[];
  column_defaults?: ColumnDefaultInfo[];
  check_constraints?: CheckConstraintInfo[];
}

export interface ProgrammableObject {
  schema_name: string;
  object_name: string;
  object_type: string;
  line_count: number;
  definition: string;
}

export interface AIRisk {
  title: string;
  category: string;
  severity: string;        // high | medium | low
  affected_objects: string;
  rationale: string;
  recommendation: string;
}

export interface AIAssessment {
  summary: string;
  complexity: string;      // Low | Medium | High
  complexity_rationale: string;
  estimated_effort: string;
  risks: AIRisk[];
  recommendations: string[];
  endpoint: string;
  success: boolean;
  error?: string | null;
}

export interface AssessmentReport {
  database: string;
  table_count: number;
  total_rows: number;
  programmable_object_count: number;
  findings: Finding[];
  readiness_score: number;
  severity_counts: Record<Severity, number>;
  tables: TableInfo[];
  programmable_objects: ProgrammableObject[];
  ai_assessment?: AIAssessment | null;
}

// --- Sizing ---
export interface SizingRequest {
  model: "dtu" | "vcore";
  environment: "dev" | "test" | "prod";
  storage_gb: number;
  dtus?: number | null;
  vcores?: number | null;
}

export interface SizingResult {
  recommended_cu: number;
  min_cu: number;
  max_cu: number;
  scale_to_zero_minutes: number | null;
  monthly_compute_cost: number;
  monthly_storage_cost: number;
  monthly_total_cost: number;
  currency: string;
  assumptions: string[];
}

// --- Schema & code ---
export interface DDLResult {
  ddl: string;
  statement_count: number;
}

export interface Translation {
  object_name: string;
  object_type: string;
  original: string;
  translated: string;
  reasoning: string;
  notes: string;
  success: boolean;
}

// --- Migration engine (execution) ---
export interface LakebaseConn {
  host: string;
  database: string;
  user: string;
  password: string;
  // Alternative to a typed password: resolve it from a secret manager.
  secret_ref?: SecretRef | null;
  port: number;
  sslmode: string;
  // Scopes a remembered/resolved target password to this migration project.
  project_id?: string;
}

export type ObjectKind =
  | "schema" | "table" | "function" | "view" | "procedure" | "trigger"
  | "constraint" | "index" | "foreign_key";

// Mirror of backend/migration/models.py POST_DATA_KINDS: plan items applied
// only AFTER the data load (constraints/indexes/FKs for bulk-load performance;
// triggers so they don't fire during the COPY).
export const POST_DATA_KINDS: ReadonlySet<ObjectKind> = new Set<ObjectKind>([
  "constraint", "index", "foreign_key", "trigger",
]);

export interface PlanItem {
  id: string;
  kind: ObjectKind;
  name: string;
  sql: string;
  original: string;
  reasoning: string;
  notes: string;
}

// Progress/result of a background plan build (POST /plan/start → poll).
export interface PlanRunState {
  run_id: string;
  status: "running" | "success" | "failed";
  objects_total: number;
  objects_done: number;
  items: PlanItem[] | null;
  error?: string | null;
}

export type ItemStatusValue = "success" | "failed" | "skipped";

export interface ItemResult {
  id: string;
  name: string;
  kind: ObjectKind;
  status: ItemStatusValue;
  error?: string | null;
  duration_ms: number;
}

export interface ApplyResponse {
  results: ItemResult[];
  success: number;
  failed: number;
  skipped: number;
}

// --- AI repair agent (autonomous validation remediation) ---
export interface RepairAttempt {
  attempt: number;
  analysis: string;
  sql: string;
  status: string; // applying|success|failed|gave_up
  error?: string | null;
}

export interface RepairItemState {
  id: string;
  name: string;
  kind: ObjectKind;
  status: string; // pending|analyzing|applying|success|failed
  gave_up: boolean; // not resolvable by SQL (e.g. row-count drift)
  reason: string; // the inconsistency being resolved
  attempts: RepairAttempt[];
  fixed_sql: string;
}

export interface RepairState {
  run_id: string;
  status: string; // running|success|partial|failed
  attempt: number;
  max_attempts: number;
  fixed: number;
  remaining: number;
  items: RepairItemState[];
  error?: string | null;
}

export interface TableProgress {
  name: string;
  target: string;
  status: string; // pending|running|success|failed
  rows_copied: number;
  total_rows: number;
  error?: string | null;
}

export interface RunState {
  run_id: string;
  status: string; // running|success|partial|failed
  tables: TableProgress[];
  error?: string | null;
}

// --- Data migration (PySpark snapshot export) ---
export interface TableRef {
  schema_name: string;
  table_name: string;
  primary_key?: string[];
}

export interface AsyncSetupResult {
  job_id?: number | null;
  job_created?: boolean;
  run_id?: number | null;
  url?: string | null;
  run_url?: string | null;
  scheduled: boolean;
  notebook_paths: string[];
  note: string;
}

export interface Artifact {
  name: string;
  filename: string;
  language: string;
  description: string;
  code: string;
}

// --- Post-migration validation ---
export type MatchStatus = "matched" | "missing" | "mismatch" | "extra";

/** One post-data object (constraint/index/FK) inside a rollup item. Constraints
 *  and indexes are compared as a count per table — there can be hundreds — so
 *  these entries carry the detail an expanded row needs to name what differs. */
export interface ObjectDiff {
  name: string;
  status: MatchStatus;
  detail: string;
  source_definition: string;
}

export interface ValidationItem {
  id: string;
  kind: ObjectKind;
  source_name: string;
  target_name: string;
  status: MatchStatus;
  severity: Severity;
  detail: string;
  recommendation: string;
  source_rows?: number | null;
  target_rows?: number | null;
  rows_approximate?: boolean; // counts are planner estimates (huge tables), not exact
  columns_missing: string[];
  columns_extra: string[];
  type_drift: string[];
  // Constraint/index/foreign-key rollups only.
  objects?: ObjectDiff[];
  objects_expected?: number;
  objects_present?: number;
  fix_sql: string;
  source_definition: string;
  remediated: boolean;
}

export interface ValidationReport {
  source_database: string;
  target_database: string;
  target_schema: string;
  generated_at: string;
  match_score: number;
  total_source: number;
  matched: number;
  missing: number;
  mismatched: number;
  extra: number;
  source_rows: number;
  target_rows: number;
  tables_compared: number;
  tables_estimated: number; // of tables_compared, how many used approximate counts
  items: ValidationItem[];
}

export interface ValidationRunState {
  run_id: string;
  status: string; // running|success|failed
  phase: string;
  tables_total: number;
  tables_done: number;
  current: string;
  report?: ValidationReport | null;
  error?: string | null;
}

export interface FixProposal {
  analysis: string;
  sql: string;
  endpoint: string;
  success: boolean;
  error?: string | null;
}

// --- Post-migration query parity ---
export type ParityStatus = "match" | "mismatch" | "error";

export interface SyntheticQuery {
  id: string;
  title: string;
  intent: string;
  category: string; // read | filter | aggregation | join | window
  source_sql: string;
  target_sql: string;
}

export interface SideResult {
  ok: boolean;
  row_count: number;
  column_names: string[];
  duration_ms: number;
  truncated: boolean;
  preview_rows: string[][]; // bounded sample of result rows (display strings)
  error?: string | null;
}

export interface RowDiff {
  row_index: number;
  kind: string; // value | source_only | target_only
  source_cells: string[];
  target_cells: string[];
  diff_columns: string[];
}

export interface QueryComparison {
  query: SyntheticQuery;
  source: SideResult;
  target: SideResult;
  status: ParityStatus;
  count_match: boolean;
  format_match: boolean;
  speedup_ratio?: number | null; // target_ms / source_ms; < 1 = target faster
  detail: string;
  mismatch_columns: string[]; // columns that disagree anywhere in the sample
  row_diffs: RowDiff[]; // differing rows in the compared prefix
}

export interface QueryParityReport {
  source_database: string;
  target_database: string;
  target_schema: string;
  generated_at: string;
  endpoint: string;
  requested: number;
  total: number;
  matched: number;
  mismatched: number;
  errored: number;
  parity_score: number;
  source_total_ms: number;
  target_total_ms: number;
  comparisons: QueryComparison[];
}

export interface GenerateQueriesResponse {
  queries: SyntheticQuery[];
  endpoint: string;
  success: boolean;
  error?: string | null;
}

export interface QueryParityRunState {
  run_id: string;
  status: string; // running|success|failed
  phase: string;
  queries_total: number;
  queries_done: number;
  current: string;
  report?: QueryParityReport | null;
  error?: string | null;
}

// --- Settings ---
export interface FmEndpoint {
  name: string;
  task?: string | null;
  ready: boolean;
}
export interface FmEndpointList {
  default: string;
  endpoints: FmEndpoint[];
  error?: string | null;
  api?: string;
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail ?? "Request failed");
  }
  return res.json() as Promise<T>;
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(path);
  if (!res.ok) throw new Error(res.statusText);
  return res.json() as Promise<T>;
}

async function send<T>(method: "PUT" | "DELETE", path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail ?? "Request failed");
  }
  return res.json() as Promise<T>;
}

// --- Projects ---
export type PhaseStatus = "not_started" | "in_progress" | "done";
export type IdentifierCase = "lowercase" | "preserve";

export interface SourceConfig {
  source_type: string;
  host: string;
  database: string;
  username: string;
  port: number;
  // Non-secret password pointer, persisted so a resumed session restores Secret mode.
  secret_ref?: SecretRef | null;
}
export interface TargetConfig {
  host: string;
  database: string;
  user: string;
  port: number;
  sslmode: string;
  secret_ref?: SecretRef | null;
}
export interface ProjectSummary {
  id: string;
  name: string;
  source_connector_id: string;
  target_host: string;
  updated_at: string;
  statuses: Record<string, PhaseStatus>;
}
export interface Project {
  id: string;
  name: string;
  source_connector_id: string;
  created_at: string;
  updated_at: string;
  source: SourceConfig;
  target: TargetConfig;
  target_schema: string;
  identifier_case: IdentifierCase;
  assessment: AssessmentReport | null;
  plan: PlanItem[] | null;
  selection: string[];
  data_options: DataOptions;
  validation: ValidationReport | null;
  query_parity: QueryParityReport | null;
  statuses: Record<string, PhaseStatus>;
  runs: unknown[];
}

export interface DataOptions {
  truncate_first: boolean;
  batch_size: number;
}

/** The single workspace the backend is bound to (CLI profile locally, the App's
 *  own workspace when deployed). Read-only — there is no in-app login. */
export interface WorkspaceStatus {
  connected: boolean;
  host?: string;
  user?: string;
  error?: string;
}

export const api = {
  testConnection: (req: ConnectionRequest) =>
    post<ConnectionResult>("/api/assessment/test-connection", req),
  scan: (req: ConnectionRequest, endpoint?: string) =>
    post<AssessmentReport>(`/api/assessment/scan${endpoint ? `?endpoint=${encodeURIComponent(endpoint)}` : ""}`, req),

  estimateSizing: (req: SizingRequest) =>
    post<SizingResult>("/api/sizing/estimate", req),

  generateDDL: (tables: TableInfo[], target_schema: string, identifier_case: IdentifierCase = "lowercase") =>
    post<DDLResult>("/api/schema/ddl", { tables, target_schema, identifier_case }),
  translate: (objects: ProgrammableObject[], endpoint?: string) =>
    post<{ translations: Translation[] }>("/api/schema/translate", { objects, endpoint }),

  generateETL: (req: Record<string, unknown>) =>
    post<{ artifacts: Artifact[] }>("/api/data/generate", req),

  listFmEndpoints: () => get<FmEndpointList>("/api/settings/fm-endpoints"),

  // --- Databricks workspace (bound at startup; read-only) ---
  dbStatus: () => get<WorkspaceStatus>("/api/databricks/status"),

  // Populate the password-source dropdowns. Both fail-soft server-side (empty
  // list on missing auth/permission), so the UI falls back to manual entry.
  listSecretScopes: () =>
    get<{ scopes: SecretScopeOption[] }>("/api/databricks/secret-scopes"),
  listSecretKeys: (scope: string) =>
    get<{ keys: string[] }>(`/api/databricks/secret-scopes/${encodeURIComponent(scope)}/keys`),
  // Inspect a secret so the form can auto-fill host/database/port when it holds a
  // full connection string. The password is never returned.
  previewSecret: (scope: string, key: string) =>
    get<SecretPreview>(
      `/api/databricks/secret-scopes/${encodeURIComponent(scope)}/keys/${encodeURIComponent(key)}/preview`,
    ),

  // --- Execution ---
  testLakebase: (req: LakebaseConn) =>
    post<{ ok: boolean; message: string }>("/api/migration/lakebase/test", req),
  buildPlan: (body: {
    tables: TableInfo[];
    programmable_objects: ProgrammableObject[];
    target_schema: string;
    identifier_case: IdentifierCase;
    translate: boolean;
    endpoint?: string;
  }) => post<{ items: PlanItem[] }>("/api/migration/plan", body),
  // Background plan build (AI translation runs past the Apps ~120s request
  // timeout): start returns a run_id, then poll planStatus to completion.
  startBuildPlan: (body: {
    tables: TableInfo[];
    programmable_objects: ProgrammableObject[];
    target_schema: string;
    identifier_case: IdentifierCase;
    translate: boolean;
    endpoint?: string;
  }) => post<{ run_id: string }>("/api/migration/plan/start", body),
  planStatus: (runId: string) => get<PlanRunState>(`/api/migration/plan/status/${runId}`),
  applyPlan: (body: { lakebase: LakebaseConn; items: PlanItem[]; stop_on_error: boolean }) =>
    post<ApplyResponse>("/api/migration/apply", body),
  startData: (body: Record<string, unknown>) =>
    post<{ run_id: string }>("/api/migration/data/start", body),
  dataStatus: (runId: string) => get<RunState>(`/api/migration/data/status/${runId}`),
  submitJob: (body: { spec: Record<string, unknown>; workspace_dir: string }) =>
    post<{ job_id: number; run_id: number; url: string | null; run_url: string | null; notebook_path: string }>("/api/migration/job/submit", body),
  scheduleJob: (body: { spec: Record<string, unknown>; workspace_dir: string; quartz_cron: string | null; timezone: string }) =>
    post<{ job_id: number; url: string | null; notebook_path: string; scheduled: boolean }>("/api/migration/job/schedule", body),
  asyncSetup: (body: { spec: Record<string, unknown>; workspace_dir: string; quartz_cron: string | null; timezone: string; run_now: boolean }) =>
    post<AsyncSetupResult>("/api/migration/async/setup", body),
  ensureSecrets: (body: {
    scope: string;
    secrets: Record<string, string>;
    // Optional connection descriptors: the backend fills a blank secret value
    // from the persisted credential store, so async setup works after a reload.
    source?: ConnectionRequest;
    lakebase?: LakebaseConn;
    source_key?: string;
    lakebase_key?: string;
  }) => post<{ scope: string; created: boolean; keys: string[] }>("/api/migration/secrets/ensure", body),

  // --- Post-migration validation ---
  startValidation: (body: {
    source: ConnectionRequest;
    lakebase: LakebaseConn;
    target_schema: string;
    identifier_case: IdentifierCase;
    // "objects" re-checks only schemas + code objects (fast, no row counts);
    // pass the current report so its table results carry over into the merge.
    scope?: "full" | "objects";
    previous?: ValidationReport | null;
    // Count huge tables by estimate (default) vs. exact COUNT(*) on every table.
    use_estimates?: boolean;
  }) => post<{ run_id: string }>("/api/validation/start", body),
  validationStatus: (runId: string) => get<ValidationRunState>(`/api/validation/status/${runId}`),
  proposeValidationFix: (body: { item: ValidationItem; target_schema: string; endpoint?: string }) =>
    post<FixProposal>("/api/validation/fix", body),
  startValidationRepair: (body: {
    lakebase: LakebaseConn;
    targets: { item: ValidationItem; prior_attempts?: RepairAttempt[] }[];
    target_schema: string;
    endpoint?: string;
    max_attempts?: number;
  }) => post<{ run_id: string }>("/api/validation/repair/start", body),
  validationRepairStatus: (runId: string) => get<RepairState>(`/api/validation/repair/status/${runId}`),

  // --- Post-migration query parity ---
  generateParityQueries: (body: {
    tables: TableInfo[];
    target_schema: string;
    identifier_case: IdentifierCase;
    count: number;
    endpoint?: string;
  }) => post<GenerateQueriesResponse>("/api/query-parity/generate", body),
  startParityRun: (body: {
    source: ConnectionRequest;
    lakebase: LakebaseConn;
    target_schema: string;
    identifier_case: IdentifierCase;
    queries: SyntheticQuery[];
  }) => post<{ run_id: string }>("/api/query-parity/run/start", body),
  parityRunStatus: (runId: string) => get<QueryParityRunState>(`/api/query-parity/run/status/${runId}`),

  // --- Projects ---
  listProjects: () => get<ProjectSummary[]>("/api/projects"),
  createProject: (name: string, source_connector_id: string) =>
    post<Project>("/api/projects", { name, source_connector_id }),
  getProject: (id: string) => get<Project>(`/api/projects/${id}`),
  updateProject: (id: string, project: Project) => send<Project>("PUT", `/api/projects/${id}`, project),
  deleteProject: (id: string) => send<{ ok: boolean }>("DELETE", `/api/projects/${id}`),
};
