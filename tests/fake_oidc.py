"""A minimal OpenID Connect provider for the tests: discovery, authorization
code flow with PKCE (S256), RS256-signed ID tokens and a JWKS endpoint.
`next_claims` decides who "signs in" on the next /authorize call."""

import base64
import hashlib
import secrets
import threading
import time
from urllib.parse import urlencode

from authlib.jose import JsonWebKey, jwt
from flask import Flask, jsonify, redirect, request
from werkzeug.serving import make_server


class FakeOIDCProvider:
    def __init__(self, client_id: str = "led-dashboard"):
        self.client_id   = client_id
        self.key         = JsonWebKey.generate_key("RSA", 2048, is_private=True)
        self.key_id      = "test-key"
        self.next_claims = {}
        self.token_overrides = {}   # merged into the next ID token (e.g. a wrong aud)
        self._codes: dict = {}
        self._server = make_server("127.0.0.1", 0, self._app())
        self.issuer  = f"http://127.0.0.1:{self._server.server_port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()

    def _app(self) -> Flask:
        app = Flask("fake-oidc")

        @app.route("/.well-known/openid-configuration")
        def discovery():
            return jsonify({
                "issuer":                 self.issuer,
                "authorization_endpoint": f"{self.issuer}/authorize",
                "token_endpoint":         f"{self.issuer}/token",
                "jwks_uri":               f"{self.issuer}/jwks",
                "response_types_supported": ["code"],
                "subject_types_supported":  ["public"],
                "id_token_signing_alg_values_supported": ["RS256"],
                "code_challenge_methods_supported": ["S256"],
            })

        @app.route("/jwks")
        def jwks():
            pub = self.key.as_dict(is_private=False)
            pub.update(kid=self.key_id, use="sig", alg="RS256")
            return jsonify({"keys": [pub]})

        @app.route("/authorize")
        def authorize():
            args = request.args
            assert args["client_id"] == self.client_id
            assert args["code_challenge_method"] == "S256"
            code = secrets.token_urlsafe(16)
            self._codes[code] = {
                "claims":         dict(self.next_claims),
                "nonce":          args.get("nonce"),
                "code_challenge": args["code_challenge"],
                "redirect_uri":   args["redirect_uri"],
            }
            return redirect(f"{args['redirect_uri']}?{urlencode({'code': code, 'state': args['state']})}")

        @app.route("/token", methods=["POST"])
        def token():
            form  = request.form
            grant = self._codes.pop(form.get("code"), None)
            if not grant or form.get("redirect_uri") != grant["redirect_uri"]:
                return jsonify({"error": "invalid_grant"}), 400
            verifier  = form.get("code_verifier", "")
            challenge = base64.urlsafe_b64encode(
                hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            if challenge != grant["code_challenge"]:
                return jsonify({"error": "invalid_grant", "error_description": "PKCE"}), 400
            now = int(time.time())
            claims = {
                "iss": self.issuer, "aud": self.client_id, "iat": now, "exp": now + 300,
                "nonce": grant["nonce"], **grant["claims"], **self.token_overrides,
            }
            id_token = jwt.encode({"alg": "RS256", "kid": self.key_id}, claims, self.key).decode()
            return jsonify({"access_token": secrets.token_urlsafe(16), "token_type": "Bearer",
                            "expires_in": 300, "id_token": id_token})

        return app
