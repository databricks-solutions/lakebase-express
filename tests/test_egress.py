"""Egress-probe gating — the probe is a debug aid and must stay off by default."""
import pytest

from backend import egress


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "True", "yes", "on", " true "])
def test_enabled_for_truthy_values(monkeypatch, value):
    monkeypatch.setenv("LBX_EGRESS_PROBE", value)
    assert egress.probe_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe", "ture"])
def test_disabled_for_everything_else(monkeypatch, value):
    """A typo must fail closed — never silently enable the probe."""
    monkeypatch.setenv("LBX_EGRESS_PROBE", value)
    assert egress.probe_enabled() is False


def test_disabled_when_unset(monkeypatch):
    monkeypatch.delenv("LBX_EGRESS_PROBE", raising=False)
    assert egress.probe_enabled() is False


def test_log_egress_ip_starts_no_thread_when_disabled(monkeypatch):
    monkeypatch.delenv("LBX_EGRESS_PROBE", raising=False)
    started: list[str] = []
    monkeypatch.setattr(egress.threading, "Thread", lambda **kw: started.append(kw) or _NoThread())
    egress.log_egress_ip()
    assert started == []


def test_log_egress_ip_starts_thread_when_enabled(monkeypatch):
    monkeypatch.setenv("LBX_EGRESS_PROBE", "true")
    started: list[dict] = []

    def _fake_thread(**kw):
        started.append(kw)
        return _NoThread()

    monkeypatch.setattr(egress.threading, "Thread", _fake_thread)
    egress.log_egress_ip()
    assert len(started) == 1
    assert started[0]["target"] is egress._probe
    assert started[0]["daemon"] is True  # must never block startup


class _NoThread:
    def start(self) -> None:
        pass
