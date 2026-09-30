import { useCallback, useEffect, useRef, useState } from "react";
import { api, type ContextBundle } from "../api";
import CodeBlock from "../components/CodeBlock";
import CopyButton from "../components/CopyButton";
import ModelBadge from "../components/ModelBadge";
import { ProgressBar } from "../components/Progress";

interface Props {
  projectId: string;
  /** Flushes the debounced autosave — the skill is built from the *saved* project. */
  onSave: () => Promise<void>;
  /** Endpoint chosen in Settings; shown as the label on model-written notes. */
  fmEndpoint: string;
}

const FILENAME = "SKILL.md";

/** Per agent: where the file goes, and what to say once it is there. The prompt is
 *  meant to be copied verbatim, so it names the skill the way that agent will see
 *  it and asks for a plan before any edit. */
const AGENTS = [
  {
    id: "claude",
    label: "Claude Code",
    path: ".claude/skills/lakebase-app-migration/SKILL.md",
    hint: "Save it at this path in your application's repo, then open Claude Code there. Use ~/.claude/skills/ instead to have it in every project.",
    prompt:
      "Use the lakebase-app-migration skill to migrate this application from SQL Server/Azure SQL to its new Databricks Lakebase (PostgreSQL) database.\n\n" +
      "Work through the skill's sections in order. Start by finding every place the app talks to the database — connection setup, SQL strings, ORM mappings, stored-procedure calls, migrations — and show me a plan of the files you would change before editing anything.\n\n" +
      "Treat anything the skill does not mention as unchanged, and never undo anything it lists under \"Do not fix these\".",
  },
  {
    id: "codex",
    label: "Codex",
    path: "lakebase-app-migration.md",
    hint: "Commit it to your application's repo and point to it from AGENTS.md, so it is picked up with the rest of your instructions.",
    prompt:
      "Read lakebase-app-migration.md, then migrate this application from SQL Server/Azure SQL to its new Databricks Lakebase (PostgreSQL) database.\n\n" +
      "Work through that file's sections in order. Start by finding every place the app talks to the database — connection setup, SQL strings, ORM mappings, stored-procedure calls, migrations — and show me a plan of the files you would change before editing anything.\n\n" +
      "Treat anything it does not mention as unchanged, and never undo anything it lists under \"Do not fix these\".",
  },
  {
    id: "cursor",
    label: "Cursor",
    path: ".cursor/rules/lakebase-app-migration.mdc",
    hint: "Save it as a project rule at this path, so it is attached to the conversation automatically.",
    prompt:
      "Following the lakebase-app-migration rule, migrate this application from SQL Server/Azure SQL to its new Databricks Lakebase (PostgreSQL) database.\n\n" +
      "Work through the rule's sections in order. Start by finding every place the app talks to the database — connection setup, SQL strings, ORM mappings, stored-procedure calls, migrations — and show me a plan of the files you would change before editing anything.\n\n" +
      "Treat anything it does not mention as unchanged, and never undo anything it lists under \"Do not fix these\".",
  },
  {
    id: "other",
    label: "Any other agent",
    path: FILENAME,
    hint: "Attach the file to the conversation, or commit it to the repo and point the agent at it.",
    prompt:
      "Read the attached SKILL.md, then migrate this application from SQL Server/Azure SQL to its new Databricks Lakebase (PostgreSQL) database.\n\n" +
      "Work through its sections in order. Start by finding every place the app talks to the database — connection setup, SQL strings, ORM mappings, stored-procedure calls, migrations — and show me a plan of the files you would change before editing anything.\n\n" +
      "Treat anything it does not mention as unchanged, and never undo anything it lists under \"Do not fix these\".",
  },
] as const;

type AgentId = (typeof AGENTS)[number]["id"];

/** Downloads the app-migration skill and shows how to hand it to an agent. */
/** "2 days ago" / "just now" for an ISO timestamp, or "" when there isn't one.
 *
 * Model notes are written once and replayed by every export, so how old they are is
 * what separates current advice from advice about a translation since replaced. An
 * absolute date would make the reader do that subtraction themselves.
 */
function relativeAge(iso: string | undefined): string {
  if (!iso) return "";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "";
  const minutes = Math.max(0, Math.round((Date.now() - then) / 60000));
  if (minutes < 2) return "just now";
  if (minutes < 60) return `${minutes} minutes ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  const days = Math.round(hours / 24);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

export default function AppMigrationSkill({ projectId, onSave, fmEndpoint }: Props) {
  const [skill, setSkill] = useState("");
  const [bundle, setBundle] = useState<ContextBundle | null>(null);
  const [agent, setAgent] = useState<AgentId>("claude");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);
  const [thinking, setThinking] = useState(false);
  const [defaultEndpoint, setDefaultEndpoint] = useState("");
  useEffect(() => {
    api.listFmEndpoints().then((r) => setDefaultEndpoint(r.default)).catch(() => {});
  }, []);
  const llm = fmEndpoint || defaultEndpoint;

  const load = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      // Save first: the backend renders from the stored row, so an unsaved edit
      // would silently be missing from the skill.
      await onSave().catch(() => {});
      const [md, json] = await Promise.all([
        api.contextSkill(projectId),
        api.contextBundle(projectId),
      ]);
      setSkill(md);
      setBundle(json);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }, [projectId, onSave]);

  /** Runs the model pass in the background and polls it — reading every object
   *  takes minutes, well past the Apps request timeout. */
  const addNotes = useCallback(async () => {
    setThinking(true);
    setError(null);
    try {
      const { run_id } = await api.startContextNotes(projectId, llm || undefined);
      for (;;) {
        await new Promise((r) => setTimeout(r, 2000));
        const state = await api.contextNotesStatus(projectId, run_id);
        if (state.status === "running") continue;
        if (state.status === "failed") setError(`${state.endpoint || "The model"} could not add notes: ${state.error}`);
        break;
      }
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setThinking(false);
    }
  }, [projectId, llm, load]);

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  // The notes are most of what makes the skill worth handing over, and asking for them
  // by hand meant most exports simply went without. They are generated on first open
  // instead, then reused by every later export — so this fires at most once per project
  // per mount, and only when there is nothing to reuse and something to read. A failed
  // run is not retried automatically: that would burn a model call per visit.
  const autoStarted = useRef<string | null>(null);
  useEffect(() => {
    if (busy || thinking || !bundle) return;
    if (bundle.ai_notes?.success) return;
    if (autoStarted.current === projectId) return;
    // Nothing translated yet — the model would have nothing to read, and the backend
    // would answer with an error the user did not ask for.
    if (!bundle.callables.some((c) => c.translated)) return;
    autoStarted.current = projectId;
    addNotes();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bundle, busy, thinking, projectId]);

  const selected = AGENTS.find((a) => a.id === agent) ?? AGENTS[0];
  const notes = bundle?.ai_notes ?? null;
  // Notes are generated once and replayed on every export, so their age is the only
  // signal that separates current advice from advice about a translation since replaced.
  const noteAge = relativeAge(notes?.generated_at);
  const counts = new Map((bundle?.sections ?? []).map((s) => [s.name, s.count]));
  // Objects, not call sites: this tool never sees the application, so finding the
  // places that call them is the agent's job. Triggers are excluded — they fire on
  // their own, so there is no call site to change.
  const called = (bundle?.callables ?? []).filter((c) => c.object_type !== "TRIGGER").length;
  const covers = (
    [
      [counts.get("columns"), "column change", "column changes"],
      [counts.get("names"), "renamed object", "renamed objects"],
      [called, "object your code calls", "objects your code calls"],
      [counts.get("rewrite_rules"), "T-SQL rewrite", "T-SQL rewrites"],
      [counts.get("gaps"), "open gap", "open gaps"],
    ] as [number | undefined, string, string][]
  )
    .filter(([n]) => typeof n === "number" && n > 0)
    .map(([n, one, many]) => `${n} ${n === 1 ? one : many}`)
    .join(" · ");

  return (
    <div className="stack">
      <section className="card">
        <h2>Let an AI agent migrate the application</h2>
        <p className="muted">
          This database moved to Lakebase; the application that talks to it still has to. Download
          the skill below, drop it into your agent, and paste the prompt — it tells the agent exactly
          how names, columns, call sites and embedded T-SQL changed, and which decisions not to undo.
        </p>
        <ProgressBar active={busy} label="Building the skill from this migration…" doneLabel="Ready" />
        {error && <div className="banner banner--err">{error}</div>}
        {!busy && covers && <p className="muted">Covers {covers}.</p>}
      </section>

      <section className="card">
        <h3>1. Save the file where your agent looks</h3>
        <div className="segmented">
          {AGENTS.map((a) => (
            <button
              key={a.id}
              className={`segmented__btn ${a.id === agent ? "segmented__btn--on" : ""}`}
              onClick={() => setAgent(a.id)}
            >
              {a.label}
            </button>
          ))}
        </div>
        <div className="pathrow">
          <code>{selected.path}</code>
          <CopyButton key={agent} text={selected.path} what="path" />
        </div>
        <p className="muted">{selected.hint}</p>

        <div className="section-head">
          <div className="savebar">
            <h3 style={{ margin: 0 }}>2. Paste this prompt</h3>
            <CopyButton key={agent} text={selected.prompt} what="prompt" />
          </div>
        </div>
        <div className="prompt">{selected.prompt}</div>
      </section>

      {bundle && bundle.provenance.completeness.length > 0 && (
        <section className="card">
          <h3>Before you hand it over</h3>
          <ul className="assumptions">
            {bundle.provenance.completeness.map((c, i) => (
              <li key={i}>{c}</li>
            ))}
          </ul>
        </section>
      )}

      <section className="card">
        <div className="savebar">
          <div>
            <h3 style={{ margin: 0 }}>The skill</h3>
            <p className="muted">
              {busy
                ? "Deriving it from this migration…"
                : `${(skill.length / 1024).toFixed(0)} KB of Markdown, derived from this migration.`}
              {notes?.success && ` Includes ${notes.notes.length} model notes${noteAge ? `, written ${noteAge}` : ""}.`}
            </p>
          </div>
          <div className="savebar__actions">
            {notes?.success && <ModelBadge endpoint={notes.endpoint}
              title="The Foundation Model that wrote the advisory notes in this skill." />}
            <button
              className={`btn btn--sm${notes?.stale_dropped ? " btn--primary" : ""}`}
              disabled={busy || thinking}
              onClick={addNotes}
              title={notes?.success
                ? "Notes are written once and reused by every export — re-run them after re-translating an object."
                : "A model reads each translated object and adds what changes for its callers."}
            >
              {thinking ? "Reading…" : notes?.success ? "Re-run model notes" : "Add model notes"}
            </button>
          </div>
        </div>
        {!!notes?.stale_dropped && !busy && (
          <div className="banner banner--warn">
            {notes.stale_dropped} model note{notes.stale_dropped === 1 ? " described" : "s described"} a
            translation that has since been replaced, so {notes.stale_dropped === 1 ? "it was" : "they were"} left
            out of the skill. Re-run the notes so the advisory section matches the objects as they are now.
          </div>
        )}
        {!notes?.success && !busy && (
          <>
            <p className="muted">
              {thinking
                ? "A model is reading each translated procedure beside its original to add what changes for the code that calls it — it catches things no rule can, like a parameter shadowed by a column. This runs once and every later export reuses it; you can leave this page."
                : "A model reads each translated procedure beside its original and adds what changes for the code that calls it, as a separate advisory section — everything else stays derived from the migration itself. It runs on its own the first time there is something translated to read."}
            </p>
            <ModelBadge endpoint={llm}
              title="The Foundation Model that would write the notes — change it in Settings." />
          </>
        )}
        <ProgressBar
          active={thinking}
          label={`Reading the translated objects with ${llm || "the model"}…`}
          doneLabel="Notes added"
        />
        {skill && <CodeBlock code={skill} language="markdown" filename={FILENAME} wrap />}
      </section>
    </div>
  );
}
