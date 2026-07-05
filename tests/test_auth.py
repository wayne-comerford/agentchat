"""Auth flow: register, login, whoami, logout, forgot/reset, validation."""
from __future__ import annotations

import os

from conftest import Client


def test_health_is_public(api):
    status, body = api.get("/health")
    assert status == 200
    assert body["ok"] is True
    assert "version" in body


def test_register_returns_token_and_workspace(register):
    client, payload = register()
    assert payload["token"]
    assert payload["token_type"] == "Bearer"
    assert payload["workspace"]["role"] == "owner"
    assert payload["user"]["username"] == client.username


def test_register_rejects_short_password(api):
    status, body = api.post(
        "/v1/auth/register",
        body={"username": f"short_{os.getpid()}", "password": "x", "workspace_name": "w"},
    )
    assert status == 409
    assert "8" in body["error"]


def test_register_rejects_duplicate_username(register, api):
    client, _ = register()
    status, body = api.post(
        "/v1/auth/register",
        body={
            "username": client.username,
            "password": "another-password",
            "workspace_name": "dup-ws",
        },
    )
    assert status == 409
    assert "exists" in body["error"]


def test_register_requires_all_fields(api):
    status, body = api.post("/v1/auth/register", body={"username": "onlyname"})
    assert status == 400


def test_whoami_requires_auth(api):
    status, _ = api.get("/v1/whoami")
    assert status == 401


def test_whoami_with_token(register):
    client, _ = register()
    status, body = client.get("/v1/whoami")
    assert status == 200
    assert body["agent"]["name"] == client.username


def test_login_roundtrip(register, server):
    client, _ = register()
    fresh = Client(server["base_url"])
    status, body = fresh.post(
        "/v1/auth/login",
        body={"username": client.username, "password": client.password},
    )
    assert status == 200
    assert body["token"]
    fresh.token = body["token"]
    status, who = fresh.get("/v1/whoami")
    assert status == 200
    assert who["agent"]["name"] == client.username


def test_login_wrong_password(register, server):
    client, _ = register()
    fresh = Client(server["base_url"])
    status, _ = fresh.post(
        "/v1/auth/login",
        body={"username": client.username, "password": "wrong-password"},
    )
    assert status == 401


def test_logout_revokes_token(register):
    client, _ = register()
    status, body = client.post("/v1/auth/logout")
    assert status == 200
    assert body["revoked"] is True
    # Token no longer works.
    status, _ = client.get("/v1/whoami")
    assert status == 401


def test_forgot_password_no_enumeration(api):
    # Unknown user returns 200 (no enumeration leak).
    status, body = api.post("/v1/auth/forgot", body={"username": "definitely-not-a-user"})
    assert status == 200
    assert body["ok"] is True


def test_reset_rejects_bad_token(api):
    status, body = api.post(
        "/v1/auth/reset",
        body={"token": "not-a-real-token", "new_password": "brand-new-password"},
    )
    assert status == 400


def test_reset_requires_min_password_length(api):
    status, _ = api.post(
        "/v1/auth/reset",
        body={"token": "whatever", "new_password": "short"},
    )
    assert status == 400
