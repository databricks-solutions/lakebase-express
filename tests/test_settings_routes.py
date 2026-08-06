"""Foundation Model endpoint discovery (backend/api/settings_routes.py).

The chat call can travel over either API (see backend/fm_params.py). On the
gateway route a model can also be addressed by its ``system.ai.<model>`` id, so
Settings offers those aliases alongside the endpoint names — but only in gateway
mode, since the serving route rejects that form.
"""
from backend.api import settings_routes
from backend.api.settings_routes import FmEndpoint, _gateway_id, _with_gateway_ids


def test_gateway_id_drops_the_databricks_prefix():
    assert _gateway_id("databricks-claude-opus-4-8") == "system.ai.claude-opus-4-8"


def test_aliases_are_offered_alongside_the_endpoint_names():
    items = _with_gateway_ids([FmEndpoint(name="databricks-claude-opus-4-8", ready=True)])
    # Both forms reach the same model, so neither replaces the other.
    assert [i.name for i in items] == [
        "databricks-claude-opus-4-8",
        "system.ai.claude-opus-4-8",
    ]


def test_non_pay_per_token_endpoints_get_no_alias():
    # Custom and external-model endpoints have no system.ai id to offer.
    items = _with_gateway_ids([FmEndpoint(name="my-custom-llm", ready=True)])
    assert [i.name for i in items] == ["my-custom-llm"]


def test_readiness_carries_over_to_the_alias():
    items = _with_gateway_ids([FmEndpoint(name="databricks-x", task="llm/v1/chat", ready=False)])
    alias = next(i for i in items if i.name.startswith("system.ai."))
    assert alias.ready is False and alias.task == "llm/v1/chat"


def test_serving_mode_lists_endpoint_names_only(monkeypatch):
    monkeypatch.setattr(settings_routes, "FM_API", "serving")
    monkeypatch.setattr(
        settings_routes,
        "workspace_client",
        lambda: _FakeWorkspace([_Endpoint("databricks-claude-opus-4-8")]),
    )
    result = settings_routes.fm_endpoints()
    assert [e.name for e in result.endpoints] == ["databricks-claude-opus-4-8"]
    assert result.api == "serving"


def test_gateway_mode_also_lists_the_system_ai_ids(monkeypatch):
    monkeypatch.setattr(settings_routes, "FM_API", "gateway")
    monkeypatch.setattr(
        settings_routes,
        "workspace_client",
        lambda: _FakeWorkspace([_Endpoint("databricks-claude-opus-4-8")]),
    )
    result = settings_routes.fm_endpoints()
    assert [e.name for e in result.endpoints] == [
        "databricks-claude-opus-4-8",
        "system.ai.claude-opus-4-8",
    ]
    assert result.api == "gateway"


def test_listing_failure_still_reports_the_default_and_api(monkeypatch):
    monkeypatch.setattr(settings_routes, "FM_API", "gateway")

    def denied():
        raise Exception("PERMISSION_DENIED: cannot list serving endpoints")

    monkeypatch.setattr(settings_routes, "workspace_client", denied)
    result = settings_routes.fm_endpoints()
    # Fail-soft: the Schema phase still works with the configured default.
    assert result.endpoints == [] and "PERMISSION_DENIED" in (result.error or "")
    assert result.api == "gateway"


class _Endpoint:
    def __init__(self, name: str, task: str = "llm/v1/chat"):
        self.name = name
        self.task = task
        self.state = None


class _FakeWorkspace:
    def __init__(self, endpoints):
        self._endpoints = endpoints

    @property
    def serving_endpoints(self):
        return self

    def list(self):
        return self._endpoints
