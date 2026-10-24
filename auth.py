"""
OpenID Connect sign-in and dashboard accounts.

Written against Pocket ID (passkey-only OIDC provider, https://pocket-id.org)
but nothing in it is specific to it: discovery, authorization code flow with
PKCE (S256), ID token checked by Authlib (signature against the provider's
JWKS, issuer, audience, expiry, nonce). Both confidential clients and Pocket
ID's public clients (no secret) are supported.

The very first account to sign in becomes admin; every later one arrives
"pending" (no access) until an admin gives it a role or removes it. The
session cookie only carries the OIDC subject: roles are read from users.json
on every request, so a demotion or removal takes effect immediately.
"""

import html
import json
import os
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path

from authlib.integrations.base_client import OAuthError
from authlib.integrations.flask_client import OAuth
from flask import Blueprint, Flask, g, jsonify, redirect, request, session, url_for


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

PENDING  = "pending"
VIEWER   = "viewer"
OPERATOR = "operator"
ADMIN    = "admin"

# Ordered by privilege: each role can do everything the ones before it can.
ROLES      = (PENDING, VIEWER, OPERATOR, ADMIN)
ROLE_RANK  = {role: rank for rank, role in enumerate(ROLES)}
ROLE_LABELS = {
    PENDING:  "En attente",
    VIEWER:   "Lecture seule",
    OPERATOR: "Opérateur",
    ADMIN:    "Administrateur",
}

SESSION_USER_KEY = "sub"
SESSION_NEXT_KEY = "next"

# Reachable without a session.
PUBLIC_PATHS = {"/auth/login", "/auth/callback", "/auth/logout"}


def has_role(role: str, required: str) -> bool:
    return ROLE_RANK.get(role, -1) >= ROLE_RANK[required]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@dataclass
class AuthSettings:
    issuer:          str
    client_id:       str
    client_secret:   str
    scopes:          str
    redirect_uri:    str
    public_base_url: str
    default_role:    str
    disabled:        bool
    session_secret:  str
    session_max_age: int

    @classmethod
    def from_env(cls) -> "AuthSettings":
        default_role = os.environ.get("OIDC_DEFAULT_ROLE", PENDING).strip().lower()
        try:
            max_age = int(os.environ.get("SESSION_MAX_AGE_SECONDS", 7 * 24 * 3600))
        except ValueError:
            max_age = 7 * 24 * 3600
        return cls(
            issuer          = os.environ.get("OIDC_ISSUER", "").strip().rstrip("/"),
            client_id       = os.environ.get("OIDC_CLIENT_ID", "").strip(),
            # Empty for a public client (Pocket ID's "Public client" option):
            # the authorization code is then protected by PKCE alone.
            client_secret   = os.environ.get("OIDC_CLIENT_SECRET", "").strip(),
            scopes          = os.environ.get("OIDC_SCOPES", "openid profile email").strip(),
            redirect_uri    = os.environ.get("OIDC_REDIRECT_URI", "").strip(),
            public_base_url = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/"),
            default_role    = default_role if default_role in ROLE_RANK else PENDING,
            disabled        = os.environ.get("DASHBOARD_AUTH", "on").strip().lower() == "off",
            session_secret  = os.environ.get("SESSION_SECRET", ""),
            session_max_age = max_age,
        )

    @property
    def configured(self) -> bool:
        return bool(self.issuer and self.client_id)

    @property
    def https(self) -> bool:
        return (self.redirect_uri or self.public_base_url).startswith("https://")


def _load_session_secret(cfg: AuthSettings, data_dir: Path) -> str:
    """SESSION_SECRET, else one generated once and kept in DATA_DIR so that a
    restart doesn't sign everybody out."""
    if cfg.session_secret:
        return cfg.session_secret
    path = data_dir / "session_secret"
    try:
        if path.exists():
            return path.read_text().strip()
        secret = secrets.token_urlsafe(48)
        path.write_text(secret)
        path.chmod(0o600)
        return secret
    except OSError:
        print(f"  WARNING: cannot persist {path}: sessions won't survive a restart")
        return secrets.token_urlsafe(48)


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

class UserError(ValueError):
    """Invalid user management request; surfaced to the API as a 400."""


@dataclass
class User:
    sub:           str
    role:          str
    email:         str   = ""
    name:          str   = ""
    created_at:    float = 0.0
    last_login_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def _display_name(claims: dict) -> str:
    for key in ("name", "preferred_username", "email"):
        value = str(claims.get(key) or "").strip()
        if value:
            return value
    return str(claims.get("sub", ""))


class UserStore:
    def __init__(self, path: Path, default_role: str = PENDING):
        self._path         = path
        self._default_role = default_role
        self._lock         = threading.Lock()
        self._users: dict  = self._load()

    def _load(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            users = [User(**entry) for entry in json.loads(self._path.read_text())]
        except (json.JSONDecodeError, OSError, TypeError):
            # Starting empty would hand admin to whoever signs in next, so a
            # corrupt file must stop the app rather than be skipped.
            print(f"  FATAL: cannot read {self._path}; fix or remove it to start over")
            raise
        return {u.sub: u for u in users}

    def _persist(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps([u.to_dict() for u in self._users.values()],
                                  ensure_ascii=False, indent=2))
        tmp.chmod(0o600)
        tmp.replace(self._path)

    @property
    def users(self) -> list:
        with self._lock:
            return sorted((User(**u.to_dict()) for u in self._users.values()),
                          key=lambda u: u.created_at)

    def get(self, sub) -> User | None:
        if not sub:
            return None
        with self._lock:
            u = self._users.get(sub)
            return User(**u.to_dict()) if u else None

    def sign_in(self, claims: dict, now: float | None = None) -> User:
        """Record a successful OIDC login, creating the account on first
        sight. Profile fields follow the provider; the role stays ours."""
        now = time.time() if now is None else now
        sub = str(claims.get("sub") or "").strip()
        if not sub:
            raise UserError("Jeton d'identité sans identifiant (sub)")
        with self._lock:
            user = self._users.get(sub)
            if user is None:
                if not self._users:
                    role = ADMIN
                    print(f"  First sign-in: {_display_name(claims)} becomes admin")
                else:
                    role = self._default_role
                user = User(sub=sub, role=role, created_at=now)
                self._users[sub] = user
            user.email         = str(claims.get("email") or "")
            user.name          = _display_name(claims)
            user.last_login_at = now
            self._persist()
            return User(**user.to_dict())

    def _admins_left_without(self, sub: str) -> int:
        return sum(1 for u in self._users.values() if u.role == ADMIN and u.sub != sub)

    def set_role(self, sub: str, role) -> User:
        role = str(role).strip().lower()
        if role not in ROLE_RANK:
            raise UserError(f"Rôle inconnu : {role!r} (attendu : {', '.join(ROLES)})")
        with self._lock:
            user = self._users.get(sub)
            if user is None:
                raise KeyError(sub)
            if user.role == ADMIN and role != ADMIN and self._admins_left_without(sub) == 0:
                raise UserError("Impossible de retirer son rôle au dernier administrateur")
            user.role = role
            self._persist()
            return User(**user.to_dict())

    def remove(self, sub: str) -> None:
        with self._lock:
            user = self._users.get(sub)
            if user is None:
                raise KeyError(sub)
            if user.role == ADMIN and self._admins_left_without(sub) == 0:
                raise UserError("Impossible de supprimer le dernier administrateur")
            del self._users[sub]
            self._persist()


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

def _page(title: str, message: str, status: int = 200,
          link: str = "/auth/login", link_label: str = "Se connecter"):
    body = f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LincStation · LED Dashboard</title>
<style>
  body {{ margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         background:#05080f; color:#cdd5e0; font-family:system-ui,sans-serif; padding:16px; }}
  main {{ max-width:420px; width:100%; background:#080d18; border:1px solid #1b2640;
         border-radius:8px; padding:1.5rem; }}
  h1 {{ font-size:1rem; letter-spacing:1.5px; text-transform:uppercase; margin:0 0 1rem; }}
  h1 em {{ color:#29b6f6; font-style:normal; }}
  h2 {{ font-size:1.1rem; margin:0 0 .5rem; }}
  p {{ color:#8a9bb0; line-height:1.5; }}
  a {{ display:inline-block; margin-top:.5rem; padding:.45rem 1rem; border-radius:4px;
      border:1px solid #29b6f6; color:#29b6f6; text-decoration:none; }}
</style></head>
<body><main><h1>LincStation <em>N2</em></h1>
<h2>{html.escape(title)}</h2><p>{html.escape(message)}</p>
<a href="{html.escape(link)}">{html.escape(link_label)}</a></main></body></html>"""
    return body, status, {"Content-Type": "text/html; charset=utf-8"}


def _safe_next(target) -> str:
    """Only same-site paths: anything else would make the login an open redirect."""
    if (not isinstance(target, str) or not target.startswith("/")
            or target.startswith("//") or "\\" in target):
        return "/"
    return target


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def init_auth(app: Flask, data_dir: Path, cfg: AuthSettings | None = None) -> UserStore:
    """Configure sessions, OIDC routes, the user API and the access guard."""
    cfg = cfg or AuthSettings.from_env()
    store = UserStore(data_dir / "users.json", cfg.default_role)

    app.secret_key = _load_session_secret(cfg, data_dir)
    app.config.update(
        SESSION_COOKIE_NAME="lincstation_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=cfg.https,
        PERMANENT_SESSION_LIFETIME=timedelta(seconds=cfg.session_max_age),
    )

    oauth = OAuth(app)
    client = None
    if cfg.configured:
        client_kwargs = {"scope": cfg.scopes, "code_challenge_method": "S256"}
        if not cfg.client_secret:
            client_kwargs["token_endpoint_auth_method"] = "none"
        client = oauth.register(
            "oidc",
            client_id=cfg.client_id,
            client_secret=cfg.client_secret or None,
            server_metadata_url=f"{cfg.issuer}/.well-known/openid-configuration",
            client_kwargs=client_kwargs,
        )

    def not_configured():
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "Authentication not configured"}), 503
        return _page("Authentification non configurée",
                     "Définissez OIDC_ISSUER et OIDC_CLIENT_ID (voir le README), ou "
                     "DASHBOARD_AUTH=off derrière un reverse proxy qui authentifie déjà.",
                     status=503, link="/", link_label="Réessayer")

    def redirect_uri() -> str:
        if cfg.redirect_uri:
            return cfg.redirect_uri
        if cfg.public_base_url:
            return f"{cfg.public_base_url}/auth/callback"
        return url_for("auth.callback", _external=True)

    bp = Blueprint("auth", __name__)

    @bp.route("/auth/login")
    def login():
        if not client:
            return not_configured()
        session[SESSION_NEXT_KEY] = _safe_next(request.args.get("next"))
        try:
            return client.authorize_redirect(redirect_uri())
        except Exception as exc:
            print(f"  WARNING: cannot reach the OIDC provider at {cfg.issuer}: {exc!r}")
            return _page("Fournisseur d'identité injoignable", cfg.issuer, status=502)

    @bp.route("/auth/callback")
    def callback():
        if not client:
            return not_configured()
        try:
            token = client.authorize_access_token()
        except OAuthError as exc:
            print(f"  WARNING: OIDC sign-in failed: {exc.error}")
            return _page("Échec de la connexion", exc.description or exc.error or "", status=400)
        except Exception as exc:
            # An ID token failing validation (signature, issuer, audience…)
            # is raised by the JOSE layer rather than as an OAuthError.
            print(f"  WARNING: OIDC sign-in failed: {exc!r}")
            return _page("Échec de la connexion", "Jeton d'identité refusé", status=400)
        try:
            user = store.sign_in(token.get("userinfo") or {})
        except UserError as exc:
            return _page("Échec de la connexion", str(exc), status=400)
        target = _safe_next(session.pop(SESSION_NEXT_KEY, "/"))
        # A fresh session on every login: nothing set before authentication
        # (by a fixation attempt, say) survives it.
        session.clear()
        session[SESSION_USER_KEY] = user.sub
        session.permanent = True
        print(f"  Signed in: {user.name} ({user.role})")
        return redirect(target)

    @bp.route("/auth/logout")
    def logout():
        session.clear()
        return _page("Déconnecté", "Vous êtes déconnecté du dashboard.")

    @bp.route("/api/me")
    def me():
        user = g.get("user")
        return jsonify({
            "auth":  "disabled" if cfg.disabled else "oidc",
            "user":  user.to_dict() if user else None,
            "roles": [{"role": r, "label": ROLE_LABELS[r]} for r in ROLES],
        })

    @bp.route("/api/users", methods=["GET"])
    def list_users():
        return jsonify({
            "users": [u.to_dict() for u in store.users],
            "roles": [{"role": r, "label": ROLE_LABELS[r]} for r in ROLES],
        })

    @bp.route("/api/users/<sub>", methods=["PUT", "DELETE"])
    def user_item(sub: str):
        try:
            if request.method == "DELETE":
                store.remove(sub)
                return jsonify({"ok": True, "sub": sub})
            data = request.get_json(silent=True)
            role = data.get("role") if isinstance(data, dict) else None
            return jsonify({"ok": True, "user": store.set_role(sub, role).to_dict()})
        except KeyError:
            return jsonify({"ok": False, "error": "Utilisateur inconnu"}), 404
        except UserError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

    app.register_blueprint(bp)

    @app.before_request
    def _auth_guard():
        if request.path in PUBLIC_PATHS:
            return None
        if cfg.disabled:
            g.user = None
            return None
        if not cfg.configured:
            return not_configured()

        is_api = request.path.startswith("/api/")
        user = store.get(session.get(SESSION_USER_KEY))
        if user is None:
            session.pop(SESSION_USER_KEY, None)
            if is_api:
                return jsonify({"ok": False, "error": "Authentication required"}), 401
            return redirect(f"/auth/login?next={_safe_next(request.path)}")
        g.user = user

        if request.path == "/api/me":
            return None
        if user.role == PENDING:
            if is_api:
                return jsonify({"ok": False, "error": "Account pending approval"}), 403
            return _page("Compte en attente",
                         f"Connecté en tant que {user.name}. Un administrateur doit vous "
                         "attribuer un rôle avant que vous puissiez utiliser le dashboard.",
                         status=403, link="/auth/logout", link_label="Se déconnecter")

        if request.path == "/api/users" or request.path.startswith("/api/users/"):
            required = ADMIN
        elif request.method in ("GET", "HEAD", "OPTIONS"):
            required = VIEWER
        else:
            required = OPERATOR
        if not has_role(user.role, required):
            return jsonify({"ok": False, "error": "Insufficient role"}), 403
        return None

    return store
