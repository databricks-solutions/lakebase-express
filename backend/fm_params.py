"""Per-model parameter policy for Foundation Model chat calls.

Serving endpoints do not all accept the same OpenAI-style sampling parameters —
e.g. ``databricks-claude-fable-5`` rejects ``temperature`` outright with
``BAD_REQUEST: Model ... does not support the temperature parameter.`` Sending a
fixed parameter set therefore breaks whole model families and silently limits
which endpoints users can pick in Settings.

``query_chat`` sends only the parameters each model accepts:

1. A static policy drops parameters known to be unsupported by the chosen
   endpoint (no wasted round-trip for known families).
2. An adaptive fallback catches the server's "does not support the X parameter"
   error for models we don't know yet, strips that parameter, and retries — so
   any chat-capable endpoint in the Settings list works without a code change.
3. A ``max_tokens`` beyond the endpoint's output window is clamped to the cap
   the server reports and retried, so callers can request a large budget and
   still work on smaller-window endpoints.

Two APIs can carry the call, selected by ``LBX_FM_API`` (see ``config.FM_API``)
or per call via ``query_chat(..., api=...)``. They reach the same model and
return the same OpenAI-style body; they differ in where the model name goes and
therefore in which names they accept:

* ``serving`` (default) — ``POST /serving-endpoints/{name}/invocations``. The
  name is in the path, so only the endpoint name works.
* ``gateway`` — ``POST /ai-gateway/mlflow/v1/chat/completions``. The name is in
  the body, and both the endpoint name and the gateway model id
  (``system.ai.claude-opus-4-8``) resolve. This is the form the AI Gateway
  console's sample request uses.
"""
from __future__ import annotations

import logging
import re

from databricks.sdk.service.serving import QueryEndpointResponse

from backend.config import FM_API, FM_API_GATEWAY, workspace_client

log = logging.getLogger("lakebase_express.fm_params")

# Endpoint-name substring -> parameters the model rejects. Extend as new
# model families land; the adaptive fallback in query_chat covers the gap
# until they're added here.
_KNOWN_UNSUPPORTED: dict[str, frozenset[str]] = {
    "claude-fable": frozenset({"temperature"}),
    "claude-opus-4-8": frozenset({"temperature"}),
}

# The server tells us exactly which parameter it refused:
#   "BAD_REQUEST: Model global.anthropic.claude-fable-5 does not support the
#    temperature parameter."
_UNSUPPORTED_PARAM_RE = re.compile(
    r"does not support the ['\"]?([A-Za-z_]+)['\"]? parameter", re.IGNORECASE
)

# Oversized max_tokens is a range error, not an unsupported-parameter error, and
# it names the endpoint's actual output window:
#   "max_new_tokens 128000 cannot be greater than max_output_tokens 8192."
_MAX_TOKENS_CAP_RE = re.compile(
    r"cannot be greater than max_output_tokens (\d+)", re.IGNORECASE
)


def allowed_params(endpoint: str, params: dict) -> dict:
    """Filter chat parameters down to what ``endpoint`` is known to accept.

    Drops None values and any parameter the endpoint's model family rejects.
    """
    lowered = endpoint.lower()
    denied: set[str] = set()
    for marker, params_denied in _KNOWN_UNSUPPORTED.items():
        if marker in lowered:
            denied |= params_denied
    return {k: v for k, v in params.items() if v is not None and k not in denied}


def unsupported_param(exc: BaseException) -> str | None:
    """Extract the rejected parameter name from a serving-endpoint error, if any."""
    match = _UNSUPPORTED_PARAM_RE.search(str(exc))
    return match.group(1) if match else None


def max_tokens_cap(exc: BaseException) -> int | None:
    """Extract the endpoint's output-token cap from a range error, if any."""
    match = _MAX_TOKENS_CAP_RE.search(str(exc))
    return int(match.group(1)) if match else None


def _to_response(res: dict) -> QueryEndpointResponse:
    """Deserialize a raw OpenAI-style chat body into the SDK response type.

    The chat response uses ``finish_reason`` but the SDK model deserializes the
    camelCase ``finishReason`` — normalize so callers can see why generation
    stopped (e.g. token-limit truncation).
    """
    for choice in res.get("choices", []):
        if "finish_reason" in choice and "finishReason" not in choice:
            choice["finishReason"] = choice["finish_reason"]
    return QueryEndpointResponse.from_dict(res)


def _query_raw(w, endpoint: str, messages, params: dict) -> QueryEndpointResponse:
    """Replicate the SDK's query() wire call, allowing request fields the SDK
    method doesn't expose yet (e.g. ``response_format`` for structured output)."""
    body = {"messages": [m.as_dict() for m in messages], **params}
    res = w.api_client.do(
        "POST",
        f"/serving-endpoints/{endpoint}/invocations",
        body=body,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    return _to_response(res)


def _query_gateway(w, endpoint: str, messages, params: dict) -> QueryEndpointResponse:
    """Query via the AI Gateway's OpenAI-compatible chat route.

    The model goes in the body as ``model``, which is what lets this route accept
    the ``system.ai.<model>`` ids shown in the AI Gateway console alongside plain
    endpoint names.
    """
    body = {
        "model": endpoint,
        "messages": [m.as_dict() for m in messages],
        **params,
    }
    res = w.api_client.do(
        "POST",
        "/ai-gateway/mlflow/v1/chat/completions",
        body=body,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    return _to_response(res)


def query_chat(endpoint: str, messages, api: str | None = None, **params):
    """Query a chat model, sending only parameters that model accepts.

    Unknown models that reject a parameter are retried without it (one retry
    per rejected parameter, so the loop is bounded by ``len(params)``).

    ``response_format`` (structured output, e.g. ``{"type": "json_schema", ...}``)
    is supported by routing around the SDK method; endpoints that reject it are
    retried without it, degrading gracefully to free-form output.

    Callers may ask for a large ``max_tokens`` (big procedure bodies): endpoints
    with a smaller output window reject the value with a range error naming
    their cap, so it is clamped to that cap and retried.

    ``api`` picks the wire route — ``"serving"`` or ``"gateway"`` (see the module
    docstring); it defaults to ``config.FM_API``. Use ``"gateway"`` to address a
    model by its ``system.ai.<model>`` id.
    """
    w = workspace_client()
    api = (api or FM_API).strip().lower()
    params = allowed_params(endpoint, params)
    while True:
        try:
            if api == FM_API_GATEWAY:
                return _query_gateway(w, endpoint, messages, params)
            if "response_format" in params:
                return _query_raw(w, endpoint, messages, params)
            return w.serving_endpoints.query(name=endpoint, messages=messages, **params)
        except Exception as exc:
            cap = max_tokens_cap(exc)
            if cap is not None and 0 < cap < params.get("max_tokens", 0):
                log.warning(
                    "Endpoint %s caps output at %d tokens — clamping max_tokens", endpoint, cap
                )
                params["max_tokens"] = cap  # == cap next round, so this can't loop
                continue
            param = unsupported_param(exc)
            if param is None and "response_format" in params and "response_format" in str(exc).lower():
                # Rejections of structured output don't always use the standard
                # "does not support the X parameter" phrasing.
                param = "response_format"
            if param is None or param not in params:
                raise
            log.warning(
                "Endpoint %s rejected parameter %r — retrying without it", endpoint, param
            )
            params.pop(param)


def chat_text(resp) -> str:
    """The final answer text of a chat response, whatever the content shape.

    Plain chat models return ``message.content`` as a string, but reasoning
    models (e.g. ``databricks-claude-fable-5``) return a *list* of content
    blocks — ``reasoning`` block(s) followed by the ``text`` block(s) with the
    actual answer. Reasoning blocks are dropped; only ``text`` parts are kept
    (joined, in order). Anything else stringifies defensively so callers'
    fail-soft paths see a readable value instead of an AttributeError.
    """
    content = resp.choices[0].message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text") or ""
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return "" if content is None else str(content)
