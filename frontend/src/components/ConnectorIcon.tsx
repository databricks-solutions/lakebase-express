import { useState } from "react";
import type { Connector } from "../connectors";

/** Connector logo with graceful fallback.
 *
 *  Tries to load `/logos/<id>.svg` (drop real brand assets into frontend/public/logos/).
 *  If none exists, falls back to a brand-colored tile with the connector's
 *  abbreviation — so the catalog always renders cleanly with or without assets.
 */
export default function ConnectorIcon({ connector, size = 40 }: { connector: Connector; size?: number }) {
  const [failed, setFailed] = useState(false);
  const radius = size * 0.22;

  if (!failed) {
    return (
      <span
        className="cicon cicon--logo"
        style={{ width: size, height: size, borderRadius: radius }}
      >
        <img
          src={connector.logo ?? `/logos/${connector.id}.svg`}
          alt={connector.name}
          width={size}
          height={size}
          onError={() => setFailed(true)}
        />
      </span>
    );
  }

  return (
    <span
      className="cicon"
      style={{
        width: size,
        height: size,
        background: connector.color,
        fontSize: size * 0.36,
        borderRadius: radius,
      }}
      aria-hidden
    >
      {connector.abbr}
    </span>
  );
}
