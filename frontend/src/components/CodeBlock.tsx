import { useState } from "react";

interface Props {
  code: string;
  language: string;
  filename?: string;
}

/** Read-only code panel with copy + download. No syntax-highlight dep — keeps the
 *  bundle small; the monospace block is enough for review/export. */
export default function CodeBlock({ code, language, filename }: Props) {
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
          <button className="btn btn--sm" onClick={copy}>{copied ? "Copied" : "Copy"}</button>
          <button className="btn btn--sm" onClick={download}>Download</button>
        </div>
      </div>
      <pre className="code__body">{code}</pre>
    </div>
  );
}
