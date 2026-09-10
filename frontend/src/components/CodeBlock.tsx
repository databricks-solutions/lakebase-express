import { ReactNode, useState } from "react";

interface Props {
  code: string;
  language: string;
  filename?: string;
  /** Replaces the download button, for panels where saving a file is not the point. */
  action?: ReactNode;
}

/** Read-only code panel with copy + download. No syntax-highlight dep — keeps the
 *  bundle small; the monospace block is enough for review/export. */
export default function CodeBlock({ code, language, filename, action }: Props) {
  const [copied, setCopied] = useState(false);

  function copy() {
    navigator.clipboard.writeText(code).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  }

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
          <button className="btn btn--sm btn--icon" onClick={copy}
                  title={copied ? "Copied" : "Copy"} aria-label={copied ? "Copied" : "Copy"}>
            <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor"
                 strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
              {copied
                ? <path d="M20 6 9 17l-5-5" />
                : <><rect x="9" y="9" width="11" height="11" rx="2" />
                    <path d="M5 15V5a2 2 0 0 1 2-2h8" /></>}
            </svg>
          </button>
          {action ?? <button className="btn btn--sm" onClick={download}>Download</button>}
        </div>
      </div>
      <pre className="code__body">{code}</pre>
    </div>
  );
}
