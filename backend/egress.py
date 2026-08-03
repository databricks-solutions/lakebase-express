"""Egress-IP probe, logged once at app startup for deployment troubleshooting.

Azure SQL's server firewall allowlists by *source IP*, and a deployed Databricks
App egresses from a different (serverless) IP than your laptop — so a scan that
works locally can fail in the app with Azure SQL error 40615 ("Client with IP
address '…' is not allowed to access the server").

On startup we log the app's public egress IP so you can read it from
``databricks apps logs`` and allowlist it on the Azure SQL firewall (or enable
"Allow Azure services"). We probe several echo services: if they disagree, the
serverless egress is rotating and pinning a single firewall rule is fragile —
prefer "Allow Azure services" or a private endpoint instead.
"""
from __future__ import annotations

import logging
import threading
import urllib.request

log = logging.getLogger("lakebase_express.egress")

# Independent echo services — querying more than one surfaces IP rotation and
# avoids depending on any single provider being reachable.
_ECHO_SERVICES = (
    "https://api.ipify.org",
    "https://checkip.amazonaws.com",
    "https://ifconfig.me/ip",
)


def _probe() -> None:
    seen: list[str] = []
    for url in _ECHO_SERVICES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "lakebase-express"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                ip = resp.read().decode().strip()
            if ip and ip not in seen:
                seen.append(ip)
        except Exception as exc:  # provider down / egress blocked — try the next one
            log.debug("Egress probe failed for %s: %s", url, exc)

    if not seen:
        log.warning("Egress IP: could not determine (no echo service reachable).")
    elif len(seen) == 1:
        log.info(
            "Egress IP: %s — allowlist this on the Azure SQL server firewall "
            "(or enable 'Allow Azure services').",
            seen[0],
        )
    else:
        log.warning(
            "Egress IP: multiple addresses seen (%s) — serverless egress is rotating; "
            "a single firewall rule is fragile, prefer 'Allow Azure services' or a "
            "private endpoint.",
            ", ".join(seen),
        )


def log_egress_ip() -> None:
    """Kick off the egress probe in a daemon thread so it never blocks startup."""
    threading.Thread(target=_probe, name="egress-probe", daemon=True).start()
