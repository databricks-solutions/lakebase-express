import { useEffect, useState } from "react";

/** Trickling progress for an operation with no real progress events: climb and
 *  decelerate toward ~92% while `active`, then snap to 100% and fade out once it
 *  finishes. Returns `visible` so callers can mount/unmount cleanly. */
function useTrickle(active: boolean) {
  const [value, setValue] = useState(0);
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    let climb: ReturnType<typeof setInterval> | undefined;
    let hide: ReturnType<typeof setTimeout> | undefined;

    if (active) {
      setVisible(true);
      setValue((v) => (v < 8 ? 8 : v)); // jump to a visible start
      climb = setInterval(() => {
        // Asymptotic approach to 92% — fast at first, never quite arriving.
        setValue((v) => (v >= 92 ? v : v + Math.max(0.5, (92 - v) * 0.06)));
      }, 240);
    } else {
      // Finished: fill to 100%, hold so the "done" state is visible, then hide.
      setValue((v) => (v > 0 ? 100 : 0));
      hide = setTimeout(() => {
        setVisible(false);
        setValue(0);
      }, 650);
    }
    return () => {
      if (climb) clearInterval(climb);
      if (hide) clearTimeout(hide);
    };
  }, [active]);

  const done = value >= 100;
  return { visible, value, done };
}

function Bar({ value, done }: { value: number; done: boolean }) {
  return (
    <div className="lbx-progress__track">
      <div
        className={`lbx-progress__bar ${done ? "is-done" : ""}`}
        style={{ width: `${Math.round(value)}%` }}
      />
    </div>
  );
}

/** Trickling progress bar. Render unconditionally with `active` — it hides
 *  itself when idle and animates to 100% when `active` goes false. */
export function ProgressBar({
  active,
  label,
  doneLabel = "Done",
}: {
  active: boolean;
  label?: string;
  doneLabel?: string;
}) {
  const { visible, value, done } = useTrickle(active);
  if (!visible) return null;
  return (
    <div className="lbx-progress" role="progressbar" aria-valuenow={Math.round(value)} aria-busy={!done}>
      <Bar value={value} done={done} />
      {(label || doneLabel) && (
        <div className="lbx-progress__label">{done ? doneLabel : label}</div>
      )}
    </div>
  );
}

// Claude-style "thinking" verbs, on-theme for a SQL Server → Lakebase migration.
const PHRASES = [
  "Lakebasing",
  "Transpiling T-SQL",
  "Reticulating schemas",
  "Postgres-ifying procedures",
  "Untangling triggers",
  "Rewriting in PL/pgSQL",
  "Consulting the migration architect",
  "Dialect-shifting",
  "Wrangling views",
  "Polishing DDL",
];

/** Trickling progress bar with a cycling, Claude-style status line. Shown while
 *  the AI agent is translating the schema & code plan; fills to 100% on finish. */
export function TranslatingStatus({ active }: { active: boolean }) {
  const { visible, value, done } = useTrickle(active);
  const [phrase, setPhrase] = useState(0);
  const [dots, setDots] = useState(1);

  useEffect(() => {
    if (done) return; // freeze the text once complete
    const word = setInterval(() => setPhrase((n) => (n + 1) % PHRASES.length), 2200);
    const dot = setInterval(() => setDots((d) => (d % 3) + 1), 420);
    return () => {
      clearInterval(word);
      clearInterval(dot);
    };
  }, [done]);

  if (!visible) return null;
  return (
    <div className="lbx-progress lbx-progress--ai" role="progressbar" aria-valuenow={Math.round(value)} aria-busy={!done}>
      <Bar value={value} done={done} />
      <div className="lbx-progress__label lbx-progress__label--ai" aria-live="polite">
        {done ? (
          <>
            <span className="lbx-check" aria-hidden>✓</span>
            <span>Translation complete</span>
          </>
        ) : (
          <>
            <span className="ai-spark" aria-hidden>✦</span>
            <span className="lbx-cycle">{PHRASES[phrase]}</span>
            <span className="lbx-dots" aria-hidden>{".".repeat(dots)}</span>
          </>
        )}
      </div>
    </div>
  );
}
