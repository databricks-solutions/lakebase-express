"""REST endpoints for the Data Migration (ETL generation) phase."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from backend.assessment.models import ConnectionRequest
from backend.connectors import lakebase_identity
from backend.connectors.credentials import LAKEBASE_NAMESPACE, resolve_effective_password
from backend.connectors.factory import build_connector
from backend.connectors.lakebase import LakebaseConnection
from backend.data_migration import capacity
from backend.data_migration.etl_generator import generate
from backend.data_migration.models import (
    CapacityLimits,
    DataGenRequest,
    DataGenResult,
    TableRef,
    TuningOverrides,
    TuningPlan,
)
from backend.data_migration.tuning import recommend
from backend.migration.models import LakebaseConnRequest

router = APIRouter(prefix="/api/data", tags=["data"])


@router.post("/generate", response_model=DataGenResult)
def generate_etl(req: DataGenRequest) -> DataGenResult:
    return DataGenResult(artifacts=generate(req))


class TuningRequest(BaseModel):
    tables: list[TableRef]
    overrides: TuningOverrides = TuningOverrides()
    # Limits from an earlier call — skips the probe when only the overrides changed.
    capacity: CapacityLimits | None = None
    source: ConnectionRequest | None = None
    lakebase: LakebaseConnRequest | None = None


class TuningResponse(BaseModel):
    capacity: CapacityLimits
    plan: TuningPlan


def _source_conn(src: ConnectionRequest | None):
    if src is None:
        return None
    password, _ = resolve_effective_password(
        src.source_type, src.host, src.database, src.username, src.project_id,
        src.password, src.secret_ref,
    )
    if not password:
        return None
    try:
        return build_connector(src.source_type, host=src.host, database=src.database,
                               username=src.username, password=password, port=src.port)
    except ValueError:  # source type not supported yet
        return None


def _target_conn(lb: LakebaseConnRequest | None):
    if lb is None or not lb.host:
        return None
    if lb.auth_mode == lakebase_identity.AUTH_DATABRICKS:
        try:
            user = lakebase_identity.current_identity()
        except lakebase_identity.IdentityAuthError:
            return None
        return LakebaseConnection(**lb.model_copy(update={"user": user, "password": ""}).conn_kwargs())
    password, _ = resolve_effective_password(
        LAKEBASE_NAMESPACE, lb.host, lb.database, lb.user, lb.project_id,
        lb.password, lb.secret_ref,
    )
    if not password:
        return None
    return LakebaseConnection(**lb.model_copy(update={"password": password}).conn_kwargs())


@router.post("/tuning", response_model=TuningResponse)
def tuning(req: TuningRequest) -> TuningResponse:
    """Recommended load tuning, from the live limits (probed here) and the overrides."""
    limits = req.capacity or capacity.probe(_source_conn(req.source), _target_conn(req.lakebase))
    return TuningResponse(capacity=limits, plan=recommend(req.tables, limits, req.overrides))
