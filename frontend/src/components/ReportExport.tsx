import { useState } from "react";
import type { ReportScope } from "../api";
import { downloadReportHtml, openReport } from "../reportExport";

interface Props {
  projectId: string;
  /** Names the project in the downloaded filename. */
  projectName: string;
  /** "assessment" exports the source scan alone; "full" the whole cycle. */
  scope?: ReportScope;
  /** Flushes the debounced autosave — the report is built from the *saved* project. */
  onSave: () => Promise<void>;
  /** Where to show a failure; the host module owns its own banner. */
  onError: (message: string) => void;
}

/** Open-in-a-tab and download-the-file actions, for a page whose purpose is the
 *  handover. A module that just wants the PDF uses `printReport` and one button. */
export default function ReportExport({
  projectId, projectName, scope = "full", onSave, onError,
}: Props) {
  const [busy, setBusy] = useState(false);

  async function run(action: () => void | Promise<void>) {
    setBusy(true);
    try {
      await onSave();
      await action();
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <button
        className="btn"
        disabled={busy}
        onClick={() => run(() => downloadReportHtml(projectId, projectName, scope))}
      >
        Download HTML
      </button>
      <button
        className="btn btn--primary"
        disabled={busy}
        onClick={() => run(() => openReport(projectId, scope))}
      >
        Open report
      </button>
    </>
  );
}
