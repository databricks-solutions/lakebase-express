"""Per-model Foundation Model parameter policy (backend/fm_params.py).

Not every serving endpoint accepts the same sampling parameters — e.g.
``databricks-claude-fable-5`` rejects ``temperature`` with BAD_REQUEST. The
helper must send only allowed parameters (static policy) and adaptively strip
whatever an unknown model rejects, so any endpoint in the Settings list works.
"""
import pytest

from backend import fm_params
from backend.fm_params import allowed_params, max_tokens_cap, query_chat, unsupported_param


# --- static policy -----------------------------------------------------------


def test_fable_endpoints_drop_temperature():
    params = allowed_params(
        "databricks-claude-fable-5", {"temperature": 0.2, "max_tokens": 2000}
    )
    assert params == {"max_tokens": 2000}


def test_other_endpoints_keep_all_params():
    params = allowed_params(
        "databricks-meta-llama-3-3-70b-instruct", {"temperature": 0.2, "max_tokens": 2000}
    )
    assert params == {"temperature": 0.2, "max_tokens": 2000}


def test_none_values_are_dropped():
    assert allowed_params("any-endpoint", {"temperature": None, "max_tokens": 100}) == {
        "max_tokens": 100
    }


# --- error-message parsing ---------------------------------------------------


def test_unsupported_param_extraction():
    exc = Exception(
        "BAD_REQUEST: Model global.anthropic.claude-fable-5 does not support "
        "the temperature parameter."
    )
    assert unsupported_param(exc) == "temperature"
    assert unsupported_param(Exception("does not support the 'top_k' parameter")) == "top_k"
    assert unsupported_param(Exception("PERMISSION_DENIED: no access")) is None


def test_max_tokens_cap_extraction():
    exc = Exception(
        "BAD_REQUEST: max_new_tokens 128000 cannot be greater than max_output_tokens 8192."
    )
    assert max_tokens_cap(exc) == 8192
    assert max_tokens_cap(Exception("PERMISSION_DENIED: no access")) is None


# --- adaptive querying -------------------------------------------------------


class _FakeServing:
    """Serving endpoint that rejects a configurable set of parameters."""

    def __init__(self, rejected: set[str]):
        self.rejected = rejected
        self.calls: list[dict] = []

    def query(self, name, messages, **params):
        self.calls.append(dict(params))
        for param in sorted(self.rejected & params.keys()):
            raise Exception(
                f"BAD_REQUEST: Model {name} does not support the {param} parameter."
            )
        return {"ok": True, "params": params}


@pytest.fixture
def serving(monkeypatch):
    holder: dict = {}

    class _W:
        @property
        def serving_endpoints(self):
            return holder["serving"]

    # These tests exercise the route-agnostic parameter policy through the serving
    # route, so pin it regardless of the configured default (which is gateway).
    monkeypatch.setattr(fm_params, "FM_API", "serving")
    monkeypatch.setattr(fm_params, "workspace_client", lambda: _W())

    def install(rejected: set[str]) -> _FakeServing:
        holder["serving"] = _FakeServing(rejected)
        return holder["serving"]

    return install


def test_known_family_is_filtered_without_a_wasted_call(serving):
    fake = serving({"temperature"})
    result = query_chat("databricks-claude-fable-5", [], temperature=0.2, max_tokens=10)
    assert result["ok"]
    # The static policy already removed temperature: exactly one call, no retry.
    assert fake.calls == [{"max_tokens": 10}]


def test_unknown_model_retries_without_rejected_param(serving):
    fake = serving({"temperature"})
    result = query_chat("databricks-future-model", [], temperature=0.2, max_tokens=10)
    assert result["ok"]
    assert fake.calls == [{"temperature": 0.2, "max_tokens": 10}, {"max_tokens": 10}]


def test_multiple_rejected_params_are_stripped_iteratively(serving):
    fake = serving({"temperature", "max_tokens"})
    result = query_chat("databricks-future-model", [], temperature=0.2, max_tokens=10)
    assert result["ok"]
    assert len(fake.calls) == 3
    assert fake.calls[-1] == {}


def test_non_parameter_errors_propagate(serving):
    fake = serving(set())

    def boom(name, messages, **params):
        raise Exception("PERMISSION_DENIED: You do not have access to this endpoint.")

    fake.query = boom
    with pytest.raises(Exception, match="PERMISSION_DENIED"):
        query_chat("databricks-claude-sonnet-5", [], temperature=0.2)


def test_error_about_param_we_did_not_send_propagates(serving):
    fake = serving(set())

    def boom(name, messages, **params):
        raise Exception("does not support the logprobs parameter")

    fake.query = boom
    # Never sent logprobs — stripping can't help, so the error must surface
    # instead of looping.
    with pytest.raises(Exception, match="logprobs"):
        query_chat("databricks-claude-sonnet-5", [], temperature=0.2)


def test_oversized_max_tokens_is_clamped_to_the_reported_cap(serving):
    fake = serving(set())
    real_query = fake.query

    def capped(name, messages, **params):
        if params.get("max_tokens", 0) > 8192:
            raise Exception(
                f"BAD_REQUEST: max_new_tokens {params['max_tokens']} cannot be "
                "greater than max_output_tokens 8192."
            )
        return real_query(name, messages, **params)

    fake.query = capped
    # Callers ask for a big output budget; smaller-window endpoints clamp it.
    result = query_chat("databricks-meta-llama-3-3-70b-instruct", [], max_tokens=128000)
    assert result["ok"] and result["params"]["max_tokens"] == 8192


def test_cap_error_not_caused_by_our_max_tokens_propagates(serving):
    fake = serving(set())

    def boom(name, messages, **params):
        raise Exception("max_new_tokens 100 cannot be greater than max_output_tokens 8192.")

    fake.query = boom
    # We already sent max_tokens at or below the reported cap — clamping can't
    # help, so the error must surface instead of looping.
    with pytest.raises(Exception, match="max_output_tokens"):
        query_chat("databricks-x", [], max_tokens=100)


# --- structured output (response_format) --------------------------------------


class _FakeApiClient:
    """Raw invocations endpoint double for the response_format path."""

    def __init__(self, reject_response_format: bool = False):
        self.reject = reject_response_format
        self.bodies: list[dict] = []

    def do(self, method, path, body=None, headers=None):
        self.bodies.append(dict(body))
        if self.reject and "response_format" in body:
            raise Exception("BAD_REQUEST: response_format is not supported for this model.")
        return {"choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": '{"ok": true}'}}]}


class _Msg:
    def as_dict(self):
        return {"role": "user", "content": "hi"}


@pytest.fixture
def raw_client(monkeypatch):
    holder: dict = {}

    class _W:
        @property
        def serving_endpoints(self):
            return holder["serving"]

        @property
        def api_client(self):
            return holder["api"]

    # response_format routes via raw invocations then falls back to the serving
    # SDK path; pin serving so the default (gateway) doesn't change the route.
    monkeypatch.setattr(fm_params, "FM_API", "serving")
    monkeypatch.setattr(fm_params, "workspace_client", lambda: _W())

    def install(reject_response_format: bool = False):
        holder["api"] = _FakeApiClient(reject_response_format)
        holder["serving"] = _FakeServing(set())
        return holder["api"], holder["serving"]

    return install


def test_response_format_routes_via_raw_invocations(raw_client):
    api, serving = raw_client()
    resp = query_chat("databricks-meta-llama-3-3-70b-instruct", [_Msg()],
                      max_tokens=10, response_format={"type": "json_object"})
    # Went through the raw wire call (the SDK method lacks response_format) and
    # deserialized into the same response type query() returns.
    assert api.bodies == [{"messages": [{"role": "user", "content": "hi"}],
                           "max_tokens": 10, "response_format": {"type": "json_object"}}]
    assert serving.calls == []
    assert resp.choices[0].message.content == '{"ok": true}'
    assert resp.choices[0].finish_reason == "stop"


def test_response_format_is_stripped_when_the_endpoint_rejects_it(raw_client):
    api, serving = raw_client(reject_response_format=True)
    result = query_chat("databricks-old-model", [], max_tokens=10,
                        response_format={"type": "json_object"})
    # First try carried response_format (rejected); the retry dropped it and
    # fell back to the regular SDK path.
    assert "response_format" in api.bodies[0]
    assert serving.calls == [{"max_tokens": 10}]
    assert result["ok"]


# --- API selection: serving vs AI Gateway -------------------------------------


class _RecordingApiClient:
    """Captures the path and body of raw wire calls."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def do(self, method, path, body=None, headers=None):
        self.calls.append((path, dict(body)))
        return {"choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "hi"}}]}


@pytest.fixture
def routes(monkeypatch):
    """Install a recording api_client plus a serving double, and return both."""
    api = _RecordingApiClient()
    fake_serving = _FakeServing(set())

    class _W:
        @property
        def serving_endpoints(self):
            return fake_serving

        @property
        def api_client(self):
            return api

    monkeypatch.setattr(fm_params, "workspace_client", lambda: _W())
    return api, fake_serving


def test_gateway_api_puts_the_model_in_the_body(routes):
    api, fake_serving = routes
    resp = query_chat("system.ai.claude-opus-4-8", [_Msg()], api="gateway", max_tokens=16)
    path, body = api.calls[0]
    # The gateway route names the model in the body, which is what lets the
    # system.ai.* ids work; the serving path is untouched.
    assert path == "/ai-gateway/mlflow/v1/chat/completions"
    assert body == {"model": "system.ai.claude-opus-4-8",
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 16}
    assert fake_serving.calls == []
    assert resp.choices[0].message.content == "hi"


def test_serving_api_puts_the_endpoint_in_the_path(routes):
    api, fake_serving = routes
    query_chat("databricks-claude-opus-4-8", [_Msg()], api="serving", max_tokens=16,
               response_format={"type": "json_object"})
    path, body = api.calls[0]
    assert path == "/serving-endpoints/databricks-claude-opus-4-8/invocations"
    assert "model" not in body


def test_api_defaults_to_the_configured_value(routes, monkeypatch):
    api, _ = routes
    monkeypatch.setattr(fm_params, "FM_API", "gateway")
    query_chat("system.ai.claude-opus-4-8", [_Msg()], max_tokens=16)
    assert api.calls[0][0] == "/ai-gateway/mlflow/v1/chat/completions"


def test_gateway_route_still_adapts_to_rejected_params(routes):
    api, _ = routes
    real_do = api.do

    def picky(method, path, body=None, headers=None):
        if "temperature" in body:
            raise Exception("BAD_REQUEST: Model x does not support the temperature parameter.")
        return real_do(method, path, body=body, headers=headers)

    api.do = picky
    query_chat("system.ai.claude-opus-4-8", [_Msg()], api="gateway", temperature=0.2)
    # The adaptive strip/retry loop wraps both routes, not just serving.
    assert "temperature" not in api.calls[-1][1]


_OPUS_5_REJECTION = (
    "INVALID_PARAMETER_VALUE: Structured output is not supported for this model because "
    "its translation requires forced tool use, which newer Anthropic models reject."
)


def _rejecting(api, message, exc_type=Exception):
    real_do = api.do

    def do(method, path, body=None, headers=None):
        if "response_format" in body:
            api.calls.append((path, dict(body)))
            raise exc_type(message)
        return real_do(method, path, body=body, headers=headers)

    api.do = do


def test_structured_output_rejection_without_the_param_name_is_stripped(routes):
    api, _ = routes
    _rejecting(api, _OPUS_5_REJECTION)
    resp = query_chat("databricks-claude-opus-5-5", [_Msg()], api="gateway", max_tokens=16,
                      response_format={"type": "json_schema", "json_schema": {}})
    assert "response_format" in api.calls[0][1]
    assert "response_format" not in api.calls[-1][1]
    assert resp.choices[0].message.content == "hi"


def test_any_bad_request_drops_structured_output(routes):
    from databricks.sdk.errors import InvalidParameterValue
    api, _ = routes
    _rejecting(api, "some future wording", InvalidParameterValue)
    query_chat("databricks-new", [_Msg()], api="gateway", max_tokens=16,
               response_format={"type": "json_object"})
    assert "response_format" not in api.calls[-1][1]


def test_rejections_are_remembered_per_endpoint(routes):
    api, _ = routes
    _rejecting(api, _OPUS_5_REJECTION)
    rf = {"type": "json_object"}
    query_chat("databricks-claude-opus-5-5", [_Msg()], api="gateway", response_format=rf)
    n = len(api.calls)
    query_chat("databricks-claude-opus-5-5", [_Msg()], api="gateway", response_format=rf)
    # The second call skips the doomed attempt; other endpoints still try it.
    assert len(api.calls) == n + 1
    assert "response_format" not in api.calls[-1][1]
    query_chat("databricks-other", [_Msg()], api="gateway", response_format=rf)
    assert "response_format" in api.calls[-2][1]


def test_unrelated_errors_without_structured_output_still_raise(routes):
    api, _ = routes

    def boom(method, path, body=None, headers=None):
        raise Exception("BAD_REQUEST: messages must not be empty")

    api.do = boom
    with pytest.raises(Exception, match="messages must not be empty"):
        query_chat("databricks-x", [_Msg()], api="gateway", max_tokens=16)
