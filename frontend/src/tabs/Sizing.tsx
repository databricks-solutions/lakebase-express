import { useState } from "react";
import { api, type SizingRequest, type SizingResult } from "../api";
import type { MigrationState } from "../App";

export default function Sizing({ state }: { state: MigrationState }) {
  const [form, setForm] = useState<SizingRequest>({
    model: "vcore",
    environment: "prod",
    storage_gb: 100,
    vcores: 4,
    dtus: 200,
  });
  const [result, setResult] = useState<SizingResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const set = (k: keyof SizingRequest, v: string | number) => setForm((f) => ({ ...f, [k]: v }));

  async function run() {
    setBusy(true);
    setError(null);
    try {
      setResult(await api.estimateSizing(form));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid">
      <section className="card">
        <h2>Source capacity</h2>
        {state.report && (
          <p className="muted">
            Assessed database <strong>{state.report.database}</strong>: {state.report.table_count} tables,{" "}
            {state.report.total_rows.toLocaleString()} rows.
          </p>
        )}

        <div className="field">
          <label>Azure SQL purchase model</label>
          <select value={form.model} onChange={(e) => set("model", e.target.value)}>
            <option value="vcore">vCore</option>
            <option value="dtu">DTU</option>
          </select>
        </div>

        {form.model === "dtu" ? (
          <div className="field">
            <label>DTUs</label>
            <input type="number" value={form.dtus ?? 0} onChange={(e) => set("dtus", Number(e.target.value))} />
          </div>
        ) : (
          <div className="field">
            <label>vCores</label>
            <input type="number" value={form.vcores ?? 0} onChange={(e) => set("vcores", Number(e.target.value))} />
          </div>
        )}

        <div className="field-row">
          <div className="field">
            <label>Storage (GB)</label>
            <input
              type="number"
              value={form.storage_gb}
              onChange={(e) => set("storage_gb", Number(e.target.value))}
            />
          </div>
          <div className="field">
            <label>Environment</label>
            <select value={form.environment} onChange={(e) => set("environment", e.target.value)}>
              <option value="prod">prod</option>
              <option value="test">test</option>
              <option value="dev">dev</option>
            </select>
          </div>
        </div>

        <div className="actions">
          <button className="btn btn--primary" disabled={busy} onClick={run}>
            {busy ? "Calculating…" : "Estimate Lakebase sizing"}
          </button>
        </div>
        {error && <div className="banner banner--err">{error}</div>}
      </section>

      <section className="card">
        <h2>Lakebase recommendation</h2>
        {!result ? (
          <p className="muted">Enter your Azure SQL capacity to map it to Lakebase CUs and cost.</p>
        ) : (
          <>
            <div className="metrics">
              <Metric label="Recommended" value={`${result.recommended_cu} CU`} />
              <Metric label="Autoscale min" value={`${result.min_cu} CU`} />
              <Metric label="Autoscale max" value={`${result.max_cu} CU`} />
              <Metric
                label="Scale-to-zero"
                value={result.scale_to_zero_minutes == null ? "disabled" : `${result.scale_to_zero_minutes} min`}
              />
            </div>
            <div className="cost">
              <Metric label="Compute / mo" value={`$${result.monthly_compute_cost.toLocaleString()}`} />
              <Metric label="Storage / mo" value={`$${result.monthly_storage_cost.toLocaleString()}`} />
              <Metric label={`Total / mo (${result.currency})`} value={`$${result.monthly_total_cost.toLocaleString()}`} />
            </div>
            <h3>Assumptions</h3>
            <ul className="assumptions">
              {result.assumptions.map((a, i) => (
                <li key={i}>{a}</li>
              ))}
            </ul>
            <p className="muted">Edit <code>backend/sizing/pricing.yaml</code> to tune ratios and prices.</p>
          </>
        )}
      </section>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="metric">
      <div className="metric__value">{value}</div>
      <div className="metric__label">{label}</div>
    </div>
  );
}
