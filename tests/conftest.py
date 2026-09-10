"""Shared test guards."""
from __future__ import annotations

import pytest

from backend import config

_REAL_LAKEBASE_ENDPOINT = config.lakebase_endpoint


@pytest.fixture(autouse=True)
def no_lakebase_endpoint_lookup(monkeypatch):
    """The run store resolves its Lakebase endpoint from the host it connects to,
    which is a live workspace call. Stubbed out by default so no test reaches the
    network; the tests for the resolver itself take it from ``lakebase_endpoint``."""
    config.lakebase_endpoint.cache_clear()
    monkeypatch.setattr(config, "lakebase_endpoint", lambda host: "")


@pytest.fixture
def lakebase_endpoint():
    """The real resolver, past the stub above."""
    _REAL_LAKEBASE_ENDPOINT.cache_clear()
    return _REAL_LAKEBASE_ENDPOINT
