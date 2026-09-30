interface Props {
  /** The serving endpoint behind whatever the panel just did. */
  endpoint: string;
  /** Extra class for placement (the badge itself is styled by `modelbadge`). */
  className?: string;
  /** Overrides the tooltip, e.g. to name what the model was used for. */
  title?: string;
}

/** "Model · <endpoint>" — shown wherever a Foundation Model did the work, so it is
 *  always clear which one, and that it can be changed in Settings. */
export default function ModelBadge({ endpoint, className = "", title }: Props) {
  if (!endpoint) return null;
  return (
    <span className={`modelbadge ${className}`.trim()}>
      <span className="modelbadge__label">Model</span>
      <span
        className="sbadge sbadge--ai"
        title={title ?? "The Foundation Model serving endpoint behind this — change it in Settings."}
      >
        {endpoint}
      </span>
    </span>
  );
}
