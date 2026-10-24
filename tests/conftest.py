import importlib
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_oidc import FakeOIDCProvider  # noqa: E402

AUTH_ENV = ("OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET", "OIDC_REDIRECT_URI",
            "PUBLIC_BASE_URL", "OIDC_DEFAULT_ROLE", "DASHBOARD_AUTH", "SESSION_SECRET",
            "ALLOWED_ORIGINS")


def load_app(monkeypatch, tmp_path, **env):
    """Import led_api afresh with the given environment and an empty DATA_DIR."""
    for key in AUTH_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    for mod in ("led_api", "auth"):
        sys.modules.pop(mod, None)
    return importlib.import_module("led_api")


@pytest.fixture
def provider():
    with FakeOIDCProvider() as p:
        yield p


@pytest.fixture
def oidc_app(monkeypatch, tmp_path, provider):
    return load_app(monkeypatch, tmp_path,
                    OIDC_ISSUER=provider.issuer, OIDC_CLIENT_ID=provider.client_id)


def sign_in(app_module, provider, claims, next_path="/"):
    """Run the whole login flow for `claims`; return the logged-in test client."""
    import requests

    client = app_module.app.test_client()
    provider.next_claims = claims
    resp = client.get(f"/auth/login?next={next_path}")
    assert resp.status_code == 302, resp.data
    # The browser follows the provider's redirect back to our callback.
    back = requests.get(resp.headers["Location"], allow_redirects=False)
    assert back.status_code == 302
    cb = urlparse(back.headers["Location"])
    done = client.get(f"{cb.path}?{cb.query}")
    return client, done
