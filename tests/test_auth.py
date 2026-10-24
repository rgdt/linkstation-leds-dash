import json
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from conftest import load_app, sign_in

ALICE = {"sub": "alice", "name": "Alice", "email": "alice@example.com"}
BOB   = {"sub": "bob",   "name": "Bob",   "email": "bob@example.com"}


def test_anonymous_requests_are_sent_to_login(oidc_app):
    c = oidc_app.app.test_client()
    resp = c.get("/")
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/auth/login?next=/")
    assert c.get("/api/state").status_code == 401
    assert c.get("/api/status").status_code == 401


def test_login_uses_pkce_and_nonce(oidc_app, provider):
    c = oidc_app.app.test_client()
    query = parse_qs(urlparse(c.get("/auth/login").headers["Location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["nonce"][0] and query["state"][0]
    assert "openid" in query["scope"][0]


def test_first_account_is_admin_next_ones_pending(oidc_app, provider):
    alice, done = sign_in(oidc_app, provider, ALICE, next_path="/")
    assert done.status_code == 302 and done.headers["Location"] == "/"
    assert alice.get("/api/me").get_json()["user"]["role"] == "admin"
    assert alice.get("/api/state").status_code == 200
    assert alice.get("/").status_code == 200

    bob, _ = sign_in(oidc_app, provider, BOB)
    assert bob.get("/api/me").get_json()["user"]["role"] == "pending"
    assert bob.get("/api/state").status_code == 403
    assert bob.get("/").status_code == 403          # "pending approval" page
    assert bob.get("/api/users").status_code == 403

    users = alice.get("/api/users").get_json()["users"]
    assert [(u["sub"], u["role"]) for u in users] == [("alice", "admin"), ("bob", "pending")]


def test_roles_gate_reads_writes_and_user_admin(oidc_app, provider):
    alice, _ = sign_in(oidc_app, provider, ALICE)
    bob, _   = sign_in(oidc_app, provider, BOB)

    assert alice.put("/api/users/bob", json={"role": "viewer"}).status_code == 200
    assert bob.get("/api/state").status_code == 200
    assert bob.post("/api/all/off", json={}).status_code == 403

    assert alice.put("/api/users/bob", json={"role": "operator"}).status_code == 200
    assert bob.post("/api/all/off", json={}).status_code == 200
    assert bob.get("/api/users").status_code == 403
    assert bob.put("/api/users/bob", json={"role": "admin"}).status_code == 403

    assert alice.put("/api/users/bob", json={"role": "boss"}).status_code == 400
    assert alice.put("/api/users/nobody", json={"role": "viewer"}).status_code == 404


def test_removed_user_loses_access_immediately(oidc_app, provider):
    alice, _ = sign_in(oidc_app, provider, ALICE)
    bob, _   = sign_in(oidc_app, provider, BOB)
    alice.put("/api/users/bob", json={"role": "operator"})
    assert bob.get("/api/state").status_code == 200

    assert alice.delete("/api/users/bob").status_code == 200
    assert bob.get("/api/state").status_code == 401
    # Signing in again only gets a pending account back.
    bob, _ = sign_in(oidc_app, provider, BOB)
    assert bob.get("/api/me").get_json()["user"]["role"] == "pending"


def test_last_admin_cannot_be_demoted_or_removed(oidc_app, provider):
    alice, _ = sign_in(oidc_app, provider, ALICE)
    assert alice.put("/api/users/alice", json={"role": "viewer"}).status_code == 400
    assert alice.delete("/api/users/alice").status_code == 400

    sign_in(oidc_app, provider, BOB)
    alice.put("/api/users/bob", json={"role": "admin"})
    assert alice.put("/api/users/alice", json={"role": "viewer"}).status_code == 200


def test_rejected_id_tokens(oidc_app, provider):
    provider.token_overrides = {"aud": "another-app"}
    _, done = sign_in(oidc_app, provider, ALICE)
    assert done.status_code == 400
    provider.token_overrides = {"iss": "https://evil.example"}
    _, done = sign_in(oidc_app, provider, ALICE)
    assert done.status_code == 400
    provider.token_overrides = {"nonce": "replayed"}
    _, done = sign_in(oidc_app, provider, ALICE)
    assert done.status_code == 400
    assert oidc_app.user_store.users == []


def test_callback_with_forged_state_is_rejected(oidc_app, provider):
    c = oidc_app.app.test_client()
    provider.next_claims = ALICE
    login = c.get("/auth/login")
    back = requests.get(login.headers["Location"], allow_redirects=False)
    cb = urlparse(back.headers["Location"])
    code = parse_qs(cb.query)["code"][0]
    assert c.get(f"/auth/callback?code={code}&state=forged").status_code == 400
    # Nor can another browser (no state in its session) finish someone else's login.
    other = oidc_app.app.test_client()
    assert other.get(f"{cb.path}?{cb.query}").status_code == 400


@pytest.mark.parametrize("target", ["//evil.example", "https://evil.example", "/\\evil.example"])
def test_login_next_is_not_an_open_redirect(oidc_app, provider, target):
    _, done = sign_in(oidc_app, provider, ALICE, next_path=target)
    assert done.headers["Location"] == "/"


def test_session_is_renewed_at_login(oidc_app, provider):
    c = oidc_app.app.test_client()
    with c.session_transaction() as s:
        s["planted"] = "x"
    provider.next_claims = ALICE
    login = c.get("/auth/login")
    back = requests.get(login.headers["Location"], allow_redirects=False)
    cb = urlparse(back.headers["Location"])
    c.get(f"{cb.path}?{cb.query}")
    with c.session_transaction() as s:
        assert "planted" not in s and s["sub"] == "alice"


def test_logout_clears_session(oidc_app, provider):
    alice, _ = sign_in(oidc_app, provider, ALICE)
    assert alice.get("/auth/logout").status_code == 200
    assert alice.get("/api/state").status_code == 401


def test_unconfigured_auth_locks_everything(monkeypatch, tmp_path):
    mod = load_app(monkeypatch, tmp_path)
    c = mod.app.test_client()
    assert c.get("/").status_code == 503
    assert c.get("/api/state").status_code == 503
    assert c.get("/auth/login").status_code == 503


def test_auth_can_be_disabled_explicitly(monkeypatch, tmp_path):
    mod = load_app(monkeypatch, tmp_path, DASHBOARD_AUTH="off")
    c = mod.app.test_client()
    assert c.get("/").status_code == 200
    assert c.get("/api/me").get_json()["auth"] == "disabled"


def test_session_cookie_flags(monkeypatch, tmp_path, provider):
    mod = load_app(monkeypatch, tmp_path, OIDC_ISSUER=provider.issuer,
                   OIDC_CLIENT_ID=provider.client_id, PUBLIC_BASE_URL="https://leds.example")
    assert mod.app.config["SESSION_COOKIE_SECURE"] is True
    assert mod.app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert mod.app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    # The public URL is an allowed origin for the CSRF guard.
    assert "https://leds.example" in mod.ALLOWED_ORIGINS


def test_users_file_is_private_and_corruption_is_fatal(oidc_app, provider, monkeypatch, tmp_path):
    sign_in(oidc_app, provider, ALICE)
    path = tmp_path / "users.json"
    assert json.loads(path.read_text())[0]["role"] == "admin"
    assert path.stat().st_mode & 0o777 == 0o600

    path.write_text("{not json")
    with pytest.raises(json.JSONDecodeError):
        load_app(monkeypatch, tmp_path, OIDC_ISSUER=provider.issuer,
                 OIDC_CLIENT_ID=provider.client_id)
