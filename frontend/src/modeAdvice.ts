// Recommends Sync now or Async mode from the selected tables' size.
import type { TableInfo } from "./api";

export type Mode = "now" | "async";

const GiB = 1024 ** 3;
// Starting thresholds, not yet calibrated against a timed in-app run (issue #29).
export const ASYNC_TOTAL_BYTES = 10 * GiB;
export const ASYNC_TABLE_BYTES = 2 * GiB;
export const ASYNC_TOTAL_ROWS = 50_000_000;

export interface ModeAdvice {
  mode: Mode;
  reason: string;
}

export function formatBytes(n: number): string {
  if (n >= GiB) return `${(n / GiB).toFixed(1)} GB`;
  if (n >= 1024 ** 2) return `${Math.round(n / 1024 ** 2)} MB`;
  return `${Math.max(1, Math.round(n / 1024))} KB`;
}

/** ``workspaceConnected`` is false only when the app knows no workspace is logged in. */
export function adviseMode(tables: TableInfo[], workspaceConnected = true): ModeAdvice | null {
  if (!tables.length) return null;
  const rows = tables.reduce((n, t) => n + t.row_count, 0);
  const bytes = tables.reduce((n, t) => n + (t.data_bytes ?? 0), 0);
  const largest = tables.reduce((a, t) => ((t.data_bytes ?? 0) > (a.data_bytes ?? 0) ? t : a));
  const largestBytes = largest.data_bytes ?? 0;
  // Scans from before sizes were recorded have no bytes; rows still count.
  const size = bytes ? `${formatBytes(bytes)} in ${plural(tables.length, "table")}`
    : `${rows.toLocaleString()} rows in ${plural(tables.length, "table")}`;

  const tooBig = "large for the in-app loader, which copies one table at a time";
  let why: string | null = null;
  if (bytes > ASYNC_TOTAL_BYTES) why = `${size} — ${tooBig}`;
  else if (largestBytes > ASYNC_TABLE_BYTES)
    why = `${largest.schema_name}.${largest.table_name} is ${formatBytes(largestBytes)} — ${tooBig}`;
  else if (rows > ASYNC_TOTAL_ROWS) why = `${rows.toLocaleString()} rows — ${tooBig}`;

  if (!why) return { mode: "now", reason: `${size} — small enough to copy here with live progress` };
  return {
    mode: "async",
    reason: workspaceConnected ? why : `${why}. Log in to a Databricks workspace to use it`,
  };
}

function plural(n: number, word: string) {
  return `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;
}
