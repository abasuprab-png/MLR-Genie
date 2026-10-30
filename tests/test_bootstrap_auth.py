from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def token_api(monkeypatch):
    import backend.main as main

    monkeypatch.delenv("MLR_GENIE_BOOTSTRAP_SECRET", raising=False)
    with TestClient(main.app) as client:
        yield main, client


TOKEN_REQUEST = {"tenant_id": "bootstrap_test_tenant", "actor_id": "bootstrap_test_actor"}


def test_bootstrap_unset_preserves_default_issuer(token_api):
    main, client = token_api
    response = client.post("/auth/token", json=TOKEN_REQUEST)
    assert response.status_code == 200
    claims = main.verify_signed_session_token(response.json()["access_token"])
    assert claims["tenant_id"] == TOKEN_REQUEST["tenant_id"]
    assert claims["actor_id"] == TOKEN_REQUEST["actor_id"]


@pytest.mark.parametrize("headers,status", [({}, 401), ({"X-MLR-Bootstrap-Secret": ""}, 401), ({"X-MLR-Bootstrap-Secret": "incorrect-test-secret"}, 403)])
def test_configured_bootstrap_rejects_missing_or_wrong_secret(token_api, monkeypatch, headers, status):
    _, client = token_api
    monkeypatch.setenv("MLR_GENIE_BOOTSTRAP_SECRET", "configured-test-secret")
    response = client.post("/auth/token", headers=headers, json=TOKEN_REQUEST)
    assert response.status_code == status
    assert "access_token" not in response.json()
    assert "configured-test-secret" not in response.text
    assert "incorrect-test-secret" not in response.text


def test_bootstrap_header_issues_a_valid_signed_token(token_api, monkeypatch):
    main, client = token_api
    monkeypatch.setenv("MLR_GENIE_BOOTSTRAP_SECRET", "configured-test-secret")
    monkeypatch.setenv("MLR_REQUIRE_SIGNED_BEARER_TOKEN", "true")
    response = client.post("/auth/token", headers={"x-mlr-bootstrap-secret": "configured-test-secret"}, json=TOKEN_REQUEST)
    assert response.status_code == 200
    claims = main.verify_signed_session_token(response.json()["access_token"])
    assert claims["tenant_id"] == TOKEN_REQUEST["tenant_id"]
    assert claims["actor_id"] == TOKEN_REQUEST["actor_id"]
    assert "configured-test-secret" not in response.text


def test_bootstrap_cannot_be_supplied_in_query_or_body(token_api, monkeypatch):
    _, client = token_api
    monkeypatch.setenv("MLR_GENIE_BOOTSTRAP_SECRET", "configured-test-secret")
    response = client.post(
        "/auth/token", params={"X-MLR-Bootstrap-Secret": "configured-test-secret"},
        json={**TOKEN_REQUEST, "bootstrap_secret": "configured-test-secret"},
    )
    assert response.status_code == 401


def test_bootstrap_rotation_takes_effect_without_restart(token_api, monkeypatch):
    _, client = token_api
    monkeypatch.setenv("MLR_GENIE_BOOTSTRAP_SECRET", "first-test-secret")
    headers = {"X-MLR-Bootstrap-Secret": "first-test-secret"}
    assert client.post("/auth/token", headers=headers, json=TOKEN_REQUEST).status_code == 200
    monkeypatch.setenv("MLR_GENIE_BOOTSTRAP_SECRET", "second-test-secret")
    assert client.post("/auth/token", headers=headers, json=TOKEN_REQUEST).status_code == 403
    assert client.post("/auth/token", headers={"X-MLR-Bootstrap-Secret": "second-test-secret"}, json=TOKEN_REQUEST).status_code == 200
