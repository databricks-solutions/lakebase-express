// Ways to get a report out of the app. Shared by the modules that export one.
import { api, type ReportScope } from "./api";

/** Opens the printable report in its own tab. */
export function openReport(projectId: string, scope: ReportScope = "full") {
  window.open(api.migrationReportUrl(projectId, scope), "_blank", "noopener");
}

/** Saves the report as a standalone .html file. */
export async function downloadReportHtml(
  projectId: string,
  projectName: string,
  scope: ReportScope = "full",
) {
  const html = await api.migrationReportHtml(projectId, scope);
  const url = URL.createObjectURL(new Blob([html], { type: "text/html" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = `${scope === "assessment" ? "assessment" : "migration"}-report-${slug(projectName)}.html`;
  a.click();
  URL.revokeObjectURL(url);
}

/** Loads the report into an offscreen frame and opens the print dialog on it, where
 *  the destination is Save as PDF.
 *
 *  The browser's print engine is what renders the PDF: doing it server-side would need
 *  WeasyPrint or a headless browser, neither of which the Databricks Apps container
 *  carries. Printing a frame rather than a new tab keeps the reader on this page. */
export async function printReport(projectId: string, scope: ReportScope = "full") {
  const html = await api.migrationReportHtml(projectId, scope);
  const frame = document.createElement("iframe");
  frame.setAttribute("aria-hidden", "true");
  frame.style.cssText = "position:fixed;right:0;bottom:0;width:0;height:0;border:0;visibility:hidden";

  await new Promise<void>((resolve, reject) => {
    frame.onload = () => resolve();
    frame.onerror = () => reject(new Error("The report could not be prepared for printing."));
    // srcdoc, not document.write: the frame is loaded before onload can be missed.
    frame.srcdoc = html;
    document.body.appendChild(frame);
  });

  const view = frame.contentWindow;
  if (!view) throw new Error("The report could not be prepared for printing.");
  // Cleaned up after the dialog closes, not before: removing the frame mid-print
  // leaves the dialog with nothing to render.
  const remove = () => frame.remove();
  view.addEventListener("afterprint", remove, { once: true });
  view.focus();
  view.print();
  // Safari and some embedded browsers never fire afterprint, so the frame would leak.
  window.setTimeout(remove, 60_000);
}

function slug(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "project";
}
