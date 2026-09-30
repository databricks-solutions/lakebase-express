import { useState } from "react";

interface Props {
  /** Text placed on the clipboard. */
  text: string;
  /** What is being copied, for the tooltip and screen readers (e.g. "path"). */
  what?: string;
}

/** The one copy affordance in the app: an icon-only button that swaps to a tick
 *  for a moment. Shared so every copy control looks and behaves the same. */
export default function CopyButton({ text, what }: Props) {
  const [copied, setCopied] = useState(false);

  function copy() {
    navigator.clipboard
      .writeText(text)
      .then(() => {
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      })
      .catch(() => {});
  }

  const label = copied ? "Copied" : what ? `Copy ${what}` : "Copy";
  return (
    <button
      className="btn btn--sm btn--icon"
      onClick={copy}
      title={label}
      aria-label={label}
    >
      <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor"
           strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
        {copied
          ? <path d="M20 6 9 17l-5-5" />
          : <><rect x="9" y="9" width="11" height="11" rx="2" />
              <path d="M5 15V5a2 2 0 0 1 2-2h8" /></>}
      </svg>
    </button>
  );
}
