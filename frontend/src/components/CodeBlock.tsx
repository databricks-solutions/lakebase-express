import { ReactNode } from "react";
import CopyButton from "./CopyButton";

interface Props {
  code: string;
  language: string;
  filename?: string;
  /** Replaces the download button, for panels where saving a file is not the point. */
  action?: ReactNode;
  /** Wrap long lines instead of scrolling sideways — for prose, not code. */
  wrap?: boolean;
}

/** Read-only code panel with copy + download. No syntax-highlight dep — keeps the
 *  bundle small; the monospace block is enough for review/export. */
export default function CodeBlock({ code, language, filename, action, wrap }: Props) {
  function download() {
    const blob = new Blob([code], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename ?? `code.${language === "python" ? "py" : language}`;
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="code">
      <div className="code__bar">
        <span className="code__lang">{filename ?? language}</span>
        <div className="code__actions">
          <CopyButton text={code} />
          {action ?? <button className="btn btn--sm" onClick={download}>Download</button>}
        </div>
      </div>
      <pre className={`code__body${wrap ? " code__body--wrap" : ""}`}>{code}</pre>
    </div>
  );
}
