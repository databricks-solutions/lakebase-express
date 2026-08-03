"""Maps Azure SQL capacity to Lakebase CUs, autoscale range, and monthly cost.

All ratios and prices come from pricing.yaml so the numbers are transparent and
editable without code changes. Every result carries the assumptions it used.
"""
from __future__ import annotations

import functools
import math
import pathlib

import yaml

from backend.sizing.models import PurchaseModel, SizingRequest, SizingResult

_PRICING_PATH = pathlib.Path(__file__).with_name("pricing.yaml")


@functools.lru_cache(maxsize=1)
def _pricing() -> dict:
    with _PRICING_PATH.open() as fh:
        return yaml.safe_load(fh)


def _peak_cu(req: SizingRequest, conv: dict) -> tuple[float, str]:
    """Derive peak CUs from the chosen purchase model. Returns (cu, assumption)."""
    if req.model is PurchaseModel.DTU:
        if not req.dtus:
            raise ValueError("DTU model selected but 'dtus' not provided.")
        cu = req.dtus / conv["dtus_per_cu"]
        return cu, f"{req.dtus} DTU ÷ {conv['dtus_per_cu']} DTU/CU = {cu:.2f} CU (peak)"
    # vCore
    if not req.vcores:
        raise ValueError("vCore model selected but 'vcores' not provided.")
    cu = req.vcores / conv["vcores_per_cu"]
    return cu, f"{req.vcores} vCore ÷ {conv['vcores_per_cu']} vCore/CU = {cu:.2f} CU (peak)"


def estimate(req: SizingRequest) -> SizingResult:
    p = _pricing()
    conv, auto, lb = p["conversion"], p["autoscale"], p["lakebase"]
    assumptions: list[str] = []

    # 1. Peak CU from Azure capacity.
    peak_cu, note = _peak_cu(req, conv)
    assumptions.append(note)

    # 2. Apply headroom -> max, floor utilisation -> min, midpoint -> steady.
    max_cu = max(conv["min_cu"], math.ceil(peak_cu * auto["peak_headroom"]))
    min_cu = max(conv["min_cu"], round(max_cu * auto["utilization_floor"]))
    recommended_cu = round((min_cu + max_cu) / 2, 1)
    assumptions.append(
        f"Autoscale: min={min_cu} (×{auto['utilization_floor']} floor), "
        f"max={max_cu} (×{auto['peak_headroom']} headroom)."
    )

    # 3. Scale-to-zero by environment.
    s2z = auto["scale_to_zero_minutes"].get(req.environment.value)
    assumptions.append(
        f"Scale-to-zero: {'disabled (prod)' if s2z is None else str(s2z) + ' min idle'} "
        f"for '{req.environment.value}'."
    )

    # 4. Cost. Compute billed on the steady-state CU assumption.
    hours = lb["hours_per_month"]
    monthly_compute = recommended_cu * lb["cu_hourly_price"] * hours
    monthly_storage = req.storage_gb * lb["storage_gb_month"]
    assumptions.append(
        f"Compute = {recommended_cu} CU × ${lb['cu_hourly_price']}/CU-hr × {hours} hr; "
        f"storage = {req.storage_gb} GB × ${lb['storage_gb_month']}/GB-mo."
    )

    return SizingResult(
        recommended_cu=recommended_cu,
        min_cu=min_cu,
        max_cu=max_cu,
        scale_to_zero_minutes=s2z,
        monthly_compute_cost=round(monthly_compute, 2),
        monthly_storage_cost=round(monthly_storage, 2),
        monthly_total_cost=round(monthly_compute + monthly_storage, 2),
        currency=p["currency"],
        assumptions=assumptions,
    )
