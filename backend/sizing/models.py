"""Sizing request/response contract."""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class PurchaseModel(str, Enum):
    DTU = "dtu"
    VCORE = "vcore"


class Environment(str, Enum):
    DEV = "dev"
    TEST = "test"
    PROD = "prod"


class SizingRequest(BaseModel):
    model: PurchaseModel
    environment: Environment = Environment.PROD
    storage_gb: float = Field(..., ge=0)

    # DTU model
    dtus: int | None = Field(default=None, ge=0)
    # vCore model
    vcores: float | None = Field(default=None, ge=0)


class SizingResult(BaseModel):
    # Capacity recommendation
    recommended_cu: float          # steady-state
    min_cu: float
    max_cu: float
    scale_to_zero_minutes: int | None

    # Cost (monthly)
    monthly_compute_cost: float
    monthly_storage_cost: float
    monthly_total_cost: float
    currency: str

    # Transparency
    assumptions: list[str]
