"""v1.1.3 — password-reset delivery channels.

Exercises the env-driven dispatcher: log channel (default) + webhook
channel. SMTP is reserved for v1.2 and asserted as a fallback to log.

These tests call `_deliver_password_reset` directly so they can mock
urllib without spinning up a server. The handler integration is covered
by `tests/test_auth.py::test_forgot_password_no_enumeration`.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import urllib.error
import urllib.request as _urllib_request

import pytest

import agentchat


class _CapturedRequest:
    """Records the URLRequest object passed to urlopen."""

    def __init__(self, real_request):
        self._req = real_request
        self.url = real_request.full_url
        self.method = real_request.get_method()
        self.headers = dict(real_request.header_items())
        self.body = real_request.data.decode("utf-8") if real_request.data else ""


class _FakeResponse:
    def __init__(self):
        self.status = 200

    def read(self, n):
        return b""


@pytest.fixture
def captured(monkeypatch):
    """Patch urlopen.urlopen to capture the outbound request."""
    captured_holder = {}

    def fake_urlopen(req, timeout=None):
        captured_holder["req"] = _CapturedRequest(req)
        captured_holder["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr(_urllib_request, "urlopen", fake_urlopen)
    return captured_holder


def test_log_channel_is_default(monkeypatch, capsys):
    monkeypatch.setattr(agentchat, "RESET_DELIVERY", "log", raising=False)
    monkeypatch.setattr(agentchat, "RESET_URL_BASE", "", raising=False)
    monkeypatch.setattr(agentchat, "RESET_WEBHOOK_URL", "", raising=False)
    agentchat._deliver_password_reset("alice", "TOKEN-XYZ")
    captured = capsys.readouterr()
    assert "PASSWORD_RESET_TOKEN: username=alice token=TOKEN-XYZ" in captured.err


def test_log_channel_includes_reset_url(monkeypatch, capsys):
    monkeypatch.setattr(agentchat, "RESET_DELIVERY", "log", raising=False)
    monkeypatch.setattr(agentchat, "RESET_URL_BASE", "https://chat.example.com", raising=False)
    monkeypatch.setattr(agentchat, "RESET_WEBHOOK_URL", "", raising=False)
    agentchat._deliver_password_reset("alice", "TOKEN-XYZ")
    captured = capsys.readouterr()
    assert "reset_url=https://chat.example.com/reset?token=TOKEN-XYZ" in captured.err


def test_webhook_channel_posts_signed_payload(monkeypatch, captured):
    monkeypatch.setattr(agentchat, "RESET_DELIVERY", "webhook", raising=False)
    monkeypatch.setattr(agentchat, "RESET_URL_BASE", "https://chat.example.com", raising=False)
    monkeypatch.setattr(agentchat, "RESET_WEBHOOK_URL", "https://relay.example.com/hook", raising=False)
    monkeypatch.setattr(agentchat, "RESET_WEBHOOK_SECRET", "shhh", raising=False)

    agentchat._deliver_password_reset("alice", "TOKEN-XYZ")

    assert "req" in captured, "urlopen was not called"
    req = captured["req"]
    assert req.method == "POST"
    assert req.url == "https://relay.example.com/hook"
    assert req.headers["Content-type"] == "application/json"
    assert req.headers["X-agentchat-event"] == "password_reset"
    assert "X-agentchat-signature" in req.headers
    body = json.loads(req.body)
    assert body["username"] == "alice"
    assert body["token"] == "TOKEN-XYZ"
    assert body["reset_url"] == "https://chat.example.com/reset?token=TOKEN-XYZ"
    assert body["expires_in_seconds"] == 3600

    # Signature must match the body with the configured secret.
    expected_sig = hmac.new(b"shhh", req.body.encode("utf-8"), hashlib.sha256).hexdigest()
    actual_sig = req.headers["X-agentchat-signature"]
    assert actual_sig == f"sha256={expected_sig}"


def test_webhook_signature_uses_empty_secret_if_unset(monkeypatch, captured):
    monkeypatch.setattr(agentchat, "RESET_DELIVERY", "webhook", raising=False)
    monkeypatch.setattr(agentchat, "RESET_URL_BASE", "", raising=False)
    monkeypatch.setattr(agentchat, "RESET_WEBHOOK_URL", "https://relay.example.com/hook", raising=False)
    monkeypatch.setattr(agentchat, "RESET_WEBHOOK_SECRET", "", raising=False)

    agentchat._deliver_password_reset("alice", "T")

    expected_sig = hmac.new(b"", captured["req"].body.encode("utf-8"), hashlib.sha256).hexdigest()
    assert captured["req"].headers["X-agentchat-signature"] == f"sha256={expected_sig}"


def test_webhook_failure_does_not_raise(monkeypatch, capsys):
    """If the relay is down, log the failure but don't bubble."""
    monkeypatch.setattr(agentchat, "RESET_DELIVERY", "webhook", raising=False)
    monkeypatch.setattr(agentchat, "RESET_URL_BASE", "", raising=False)
    monkeypatch.setattr(agentchat, "RESET_WEBHOOK_URL", "https://relay.example.com/hook", raising=False)
    monkeypatch.setattr(agentchat, "RESET_WEBHOOK_SECRET", "", raising=False)

    def boom(req, timeout=None):
        raise urllib.error.URLError("relay down")

    monkeypatch.setattr(_urllib_request, "urlopen", boom)
    # Must not raise.
    agentchat._deliver_password_reset("alice", "T")
    captured = capsys.readouterr()
    assert "RESET_WEBHOOK failed" in captured.err
    assert "URLError" in captured.err


def test_webhook_channel_skipped_when_no_url(monkeypatch, capsys):
    """DELIVERY=webhook without URL falls back to log so the operator
    at least has the token."""
    monkeypatch.setattr(agentchat, "RESET_DELIVERY", "webhook", raising=False)
    monkeypatch.setattr(agentchat, "RESET_URL_BASE", "", raising=False)
    monkeypatch.setattr(agentchat, "RESET_WEBHOOK_URL", "", raising=False)
    agentchat._deliver_password_reset("alice", "T")
    captured = capsys.readouterr()
    assert "PASSWORD_RESET_TOKEN" in captured.err


def test_smtp_channel_falls_back_to_log(monkeypatch, capsys):
    """v1.2 reserves SMTP; v1.1.3 falls back to log when configured."""
    monkeypatch.setattr(agentchat, "RESET_DELIVERY", "smtp", raising=False)
    monkeypatch.setattr(agentchat, "RESET_URL_BASE", "", raising=False)
    monkeypatch.setattr(agentchat, "RESET_WEBHOOK_URL", "", raising=False)
    agentchat._deliver_password_reset("alice", "T")
    captured = capsys.readouterr()
    assert "reserved for v1.2" in captured.err
    assert "PASSWORD_RESET_TOKEN" in captured.err


def test_webhook_timeout_respected(monkeypatch, captured):
    monkeypatch.setattr(agentchat, "RESET_DELIVERY", "webhook", raising=False)
    monkeypatch.setattr(agentchat, "RESET_URL_BASE", "", raising=False)
    monkeypatch.setattr(agentchat, "RESET_WEBHOOK_URL", "https://relay.example.com/hook", raising=False)
    monkeypatch.setattr(agentchat, "RESET_WEBHOOK_SECRET", "", raising=False)
    monkeypatch.setattr(agentchat, "RESET_WEBHOOK_TIMEOUT", 2.5, raising=False)

    agentchat._deliver_password_reset("alice", "T")
    assert captured["timeout"] == 2.5


def test_env_vars_loaded_at_import():
    """Spot-check that the env vars get read into module constants."""
    # We can't safely reload the module here, but we can verify the
    # constants exist and have sensible defaults.
    assert isinstance(agentchat.RESET_DELIVERY, str)
    assert isinstance(agentchat.RESET_WEBHOOK_TIMEOUT, (int, float))
    assert isinstance(agentchat.RESET_URL_BASE, str)