import { MODULES, type ModuleId } from "./ProjectSidebar";
import { LAKEBASE_DESTINATION, type Connector } from "../connectors";

interface Props {
  source: Connector;
  done: Record<ModuleId, boolean>;
  onOpen: (id: ModuleId) => void;
}

/** The "Migration project" hub: shows the end-to-end journey to a migrated
 *  Lakebase database, each step's status, and a single Continue action — while
 *  every step stays independently openable. */
export default function Overview({ source, done, onOpen }: Props) {
  const steps = MODULES.filter((m) => m.step > 0);
  const next = steps.find((m) => !done[m.id]) ?? steps[steps.length - 1];

  return (
    <div className="overview">
        <div className="overview__intro card">
          <h2>Migrate {source.name} → {LAKEBASE_DESTINATION.name}</h2>
          <p className="muted">
            Work through the steps below to assess the source, move schema and data into Lakebase, and
            finish by running the migration — in-app, or as a Databricks snapshot job you can re-run or
            schedule. Steps are independent — jump to any of them — and this page tracks your progress.
          </p>
          <button className="btn btn--primary btn--lg" onClick={() => onOpen(next.id)}>
            {steps.every((m) => done[m.id]) ? "Review steps" : `Continue → ${next.label}`}
          </button>
        </div>

        <ol className="journey">
          {steps.map((m) => {
            const isDone = done[m.id];
            const isNext = m.id === next.id && !isDone;
            return (
              <li key={m.id} className={`journey__item ${isNext ? "journey__item--next" : ""}`}>
                <button
                  className="journey__row"
                  onClick={() => onOpen(m.id)}
                  aria-label={`Open ${m.label}`}
                >
                  <span className={`journey__num ${isDone ? "journey__num--done" : ""}`}>
                    {isDone ? "✓" : m.step}
                  </span>
                  <div className="journey__main">
                    <div className="journey__label">
                      {m.label}
                      {isNext && <span className="pill pill--low">Next</span>}
                      {isDone && <span className="pill pill--info">Done</span>}
                    </div>
                    <div className="journey__desc">{m.desc}</div>
                  </div>
                  <span className={`journey__open ${isNext ? "journey__open--next" : ""}`} aria-hidden>
                    <ArrowRight />
                  </span>
                </button>
              </li>
            );
          })}
        </ol>
    </div>
  );
}

function ArrowRight() {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M5 12h14M13 6l6 6-6 6" />
    </svg>
  );
}
