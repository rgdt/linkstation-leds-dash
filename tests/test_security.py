"""Hardening from the security audit: CSRF guard, static files, input validation."""

import pytest

from conftest import load_app


@pytest.fixture
def client(monkeypatch, tmp_path):
    return load_app(monkeypatch, tmp_path, DASHBOARD_AUTH="off").app.test_client()


def test_source_and_data_files_are_not_served(client):
    assert client.get("/static/led_api.py").status_code == 404
    assert client.get("/static/presets.json").status_code == 404


def test_cross_site_writes_are_rejected(client):
    assert client.post("/api/all/off", data="{}", content_type="text/plain").status_code == 415
    assert client.post("/api/preset/x").status_code == 415
    assert client.post("/api/all/off", json={}, headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/all/off", json={}, headers={"Origin": "null"}).status_code == 403
    assert client.delete("/api/preset/x", headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/all/off", json={}, headers={"Origin": "http://localhost"}).status_code == 200


def test_security_headers(client):
    resp = client.get("/")
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"


def test_preset_names_are_bounded(client):
    assert client.post("/api/preset/" + "a" * 65, json={}).status_code == 400
    assert client.post("/api/preset/ok", json={}).status_code == 200


def test_binding_params_are_sanitized(client):
    resp = client.post("/api/binding", json={
        "event": "idle", "animation": "ledbar_flash", "id": "x' onclick=1",
        "params": {"on_ms": 0, "off_ms": -5, "color": "zz", "evil": 1},
        "priority": "high", "targets": "ledbar", "on_exit": "nope",
    })
    assert resp.status_code == 201
    b = resp.get_json()["binding"]
    assert b["params"] == {"on_ms": 50, "off_ms": 50, "color": [255, 0, 0]}
    assert b["priority"] == 5 and b["on_exit"] == {"type": "off"}
    assert b["id"] != "x' onclick=1"
    assert client.post("/api/binding", json={"event": "nope", "animation": "sweep"}).status_code == 400
    assert client.post("/api/ledbar", json={"brightness": "x"}).status_code == 400
