"""Local ChatGPT plan authorization preflight; never sends inference requests.

One account registration per private storage directory. This operator CLI is
separate from task authentication and the workflow's model adapter. Tokens are
not returned by status/models, and no existing Codex credentials are read.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import stat
import tempfile
import time
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

ISSUER = "https://auth.openai.com"
AUTHORIZE = f"{ISSUER}/api/accounts/authorize"
TOKEN = f"{ISSUER}/api/accounts/oauth/token"
JWKS = f"{ISSUER}/.well-known/jwks.json"
RESOURCE = "https://api.openai.com/v1"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = f"openid profile email offline_access resource.invoke {PLAN_SCOPE}"
CALLBACK = "/auth/callback"


class AuthorizationError(Exception):
    """Only fixed, public error codes may cross the CLI boundary."""


class PrivateStore:
    def __init__(self, root: Path):
        self.root = root.absolute()
        for parent in (self.root, *self.root.parents):
            if parent.is_symlink():
                raise AuthorizationError("unsafe_storage_path")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        descriptor = os.open(
            self.root / "session.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AuthorizationError("authorization_busy") from None
            yield
        finally:
            os.close(descriptor)

    def read(self, name: str) -> dict[str, Any]:
        path = self.root / name
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {}
        with os.fdopen(descriptor) as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
                raise AuthorizationError("unsafe_storage_permissions")
            value = json.load(source)
        if not isinstance(value, dict):
            raise AuthorizationError("invalid_local_session")
        return value

    def write(self, name: str, value: dict[str, Any]) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=self.root)
        try:
            with os.fdopen(descriptor, "w") as target:
                os.fchmod(target.fileno(), 0o600)
                json.dump(value, target)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.root / name)
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def host_id(self) -> str:
        existing = self.read("host.json")
        if existing:
            host = existing.get("ext_agent_host_id")
            if not isinstance(host, str) or not host.startswith("urn:uuid:"):
                raise AuthorizationError("invalid_local_host")
            return host
        host = f"urn:uuid:{uuid.uuid4()}"
        self.write("host.json", {"ext_agent_host_id": host})
        return host


def authorization_url(client_id: str, host_id: str, redirect_uri: str) -> tuple[str, str, str, str]:
    state, nonce, verifier = (secrets.token_urlsafe(48) for _ in range(3))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    parameters = {
        "client_id": client_id,
        "ext_agent_host_id": host_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "resource": RESOURCE,
        "state": state,
        "nonce": nonce,
        "code_challenge_method": "S256",
        "code_challenge": challenge.decode().rstrip("="),
    }
    if client_id == "dynamic_agent_client":
        parameters["agent_name_hint"] = "RepoPilot"
    return f"{AUTHORIZE}?{urlencode(parameters)}", state, nonce, verifier


def validate_callback(path: str, state: str, client_id: str) -> tuple[str, str]:
    parsed = urlsplit(path)
    if parsed.path != CALLBACK or parsed.scheme or parsed.netloc or parsed.fragment:
        raise AuthorizationError("invalid_callback")
    try:
        query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=20)
    except ValueError:
        raise AuthorizationError("invalid_callback") from None
    if any(len(values) != 1 for values in query.values()):
        raise AuthorizationError("invalid_callback")
    returned_state = query.get("state", [""])[0]
    if not hmac.compare_digest(returned_state.encode(), state.encode()):
        raise AuthorizationError("invalid_callback_state")
    if "error" in query:
        raise AuthorizationError("authorization_declined")
    issued = query.get("client_id", [""])[0]
    if client_id == "dynamic_agent_client":
        if not issued or issued == "dynamic_agent_client":
            raise AuthorizationError("registration_incomplete")
    elif issued and issued != client_id:
        raise AuthorizationError("registration_mismatch")
    else:
        issued = client_id
    code = query.get("code", [""])[0]
    if not code:
        raise AuthorizationError("authorization_code_missing")
    return code, issued


def http_json(client: httpx.Client, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    response = client.request(method, url, **kwargs)
    if response.status_code != 200:
        # Provider text may echo credentials, callback URLs, or user account data.
        raise AuthorizationError(f"provider_http_{response.status_code}")
    value = response.json()
    if not isinstance(value, dict):
        raise AuthorizationError("invalid_provider_response")
    return value


def verify_identity(
    client: httpx.Client, id_token: str, client_id: str, nonce: str
) -> dict[str, Any]:
    try:
        import jwt
    except ImportError:
        raise AuthorizationError("install_oauth_dependency") from None
    keys = http_json(client, "GET", JWKS)
    try:
        header = jwt.get_unverified_header(id_token)
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise AuthorizationError("invalid_identity")
        matching = [
            key for key in keys.get("keys", [])
            if isinstance(key, dict) and key.get("kid") == header["kid"]
        ]
        if len(matching) != 1:
            raise AuthorizationError("invalid_identity")
        key = jwt.PyJWK.from_dict(matching[0], algorithm="RS256")
        identity = jwt.decode(
            id_token,
            key.key,
            algorithms=["RS256"],
            audience=client_id,
            issuer=ISSUER,
            leeway=5,
            options={"require": ["iss", "aud", "sub", "exp", "iat", "nonce"]},
        )
        returned_nonce = identity.get("nonce")
        if not isinstance(returned_nonce, str) or not hmac.compare_digest(
            returned_nonce.encode(), nonce.encode()
        ):
            raise AuthorizationError("invalid_identity")
        if not isinstance(identity["sub"], str) or not identity["sub"]:
            raise AuthorizationError("invalid_identity")
    except (jwt.PyJWTError, KeyError, TypeError, ValueError):
        raise AuthorizationError("invalid_identity") from None
    return identity


def save_exchange(
    client: httpx.Client,
    store: PrivateStore,
    *,
    code: str,
    client_id: str,
    redirect_uri: str,
    verifier: str,
    nonce: str,
) -> dict[str, Any]:
    # Preserve the issued registration even if its short-lived code expires.
    store.write("registration.json", {"client_id": client_id})
    tokens = http_json(
        client,
        "POST",
        TOKEN,
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "resource": RESOURCE,
        },
    )
    if not isinstance(tokens.get("id_token"), str):
        raise AuthorizationError("invalid_token_response")
    identity = verify_identity(client, tokens["id_token"], client_id, nonce)
    prior = store.read("session.json")
    if prior and (prior.get("subject"), prior.get("client_id")) != (identity["sub"], client_id):
        raise AuthorizationError("account_mismatch")
    scope = tokens.get("scope")
    if not isinstance(scope, str):
        raise AuthorizationError("invalid_token_response")
    scopes = scope.split()
    expires = tokens.get("expires_in")
    if (
        not isinstance(expires, int)
        or isinstance(expires, bool)
        or expires <= 0
        or tokens.get("token_type", "").lower() != "bearer"
        or not isinstance(tokens.get("access_token"), str)
        or not tokens["access_token"]
    ):
        raise AuthorizationError("invalid_token_response")
    session = {
        "client_id": client_id,
        "subject": identity["sub"],
        "issuer": ISSUER,
        "ext_agent_host_id": store.host_id(),
        "scopes": scopes,
        "saved_at": time.time(),
        "expires_at": time.time() + expires,
        **{
            name: tokens[name]
            for name in ("access_token", "refresh_token", "id_token") if name in tokens
        },
    }
    store.write("session.json", session)
    return session


def safe_status(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "connected": bool(session),
        "plan_usage_authorized": PLAN_SCOPE in session.get("scopes", []),
        "access_token_current": bool(session) and session.get("expires_at", 0) > time.time(),
        "workflow_adapter_connected": False,
        "inference_requests": 0,
    }


def available_models(client: httpx.Client, session: dict[str, Any]) -> list[dict[str, str]]:
    if PLAN_SCOPE not in session.get("scopes", []):
        raise AuthorizationError("plan_usage_not_authorized")
    if session.get("expires_at", 0) <= time.time() + 30:
        raise AuthorizationError("sign_in_again_to_renew")
    catalog = http_json(
        client,
        "GET",
        f"{RESOURCE}/models",
        headers={"Authorization": f"Bearer {session['access_token']}"},
    )
    if not isinstance(catalog.get("models"), list):
        raise AuthorizationError("invalid_model_catalog")
    return [
        {"slug": row["slug"], "display_name": row["display_name"]}
        for row in catalog["models"]
        if isinstance(row, dict)
        and row.get("visibility") == "list"
        and isinstance(row.get("slug"), str)
        and isinstance(row.get("display_name"), str)
    ]


def login(client: httpx.Client, store: PrivateStore, port: int, timeout: int) -> dict[str, Any]:
    registration = store.read("registration.json")
    client_id = registration.get("client_id", "dynamic_agent_client")
    if not isinstance(client_id, str) or not client_id:
        raise AuthorizationError("invalid_local_registration")
    state = nonce = verifier = authorize_url = ""
    outcome: list[tuple[str, str] | AuthorizationError] = []

    class Handler(BaseHTTPRequestHandler):
        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *_args: Any) -> None:
            pass  # The default HTTP logger would print authorization codes.

        def reply(self, status: int, body: str, location: str | None = None) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"
            )
            self.send_header("Content-Length", str(len(payload)))
            if location:
                self.send_header("Location", location)
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            if self.headers.get("Host") != f"127.0.0.1:{server.server_port}":
                self.reply(400, "Invalid callback host.")
                return
            if self.path == "/":
                self.reply(200, '<h1>RepoPilot</h1><a href="/start">Continue with ChatGPT</a>')
            elif self.path == "/start":
                self.reply(302, "Continue in your browser.", authorize_url)
            elif urlsplit(self.path).path == CALLBACK:
                try:
                    result = validate_callback(self.path, state, client_id)
                except AuthorizationError as error:
                    # An invalid state cannot consume the actual user's attempt.
                    if str(error) == "authorization_declined":
                        outcome.append(error)
                    self.reply(400, "Authorization was not completed. Return to RepoPilot.")
                else:
                    outcome.append(result)
                    self.reply(200, "Authorization received. Return to RepoPilot for verification.")
            else:
                self.reply(404, "Not found.")

    class PrivateHTTPServer(HTTPServer):
        def handle_error(self, _request: Any, _client_address: Any) -> None:
            pass  # Suppress default request exception logging at this credential boundary.

    with PrivateHTTPServer(("127.0.0.1", port), Handler) as server:
        server.timeout = 1
        redirect_uri = f"http://127.0.0.1:{server.server_port}{CALLBACK}"
        authorize_url, state, nonce, verifier = authorization_url(
            client_id, store.host_id(), redirect_uri
        )
        print(
            json.dumps({"continue_with_chatgpt": f"http://127.0.0.1:{server.server_port}/"}),
            flush=True,
        )
        deadline = time.monotonic() + timeout
        while not outcome and time.monotonic() < deadline:
            server.handle_request()
    if not outcome:
        raise AuthorizationError("authorization_timed_out")
    result = outcome[0]
    if isinstance(result, AuthorizationError):
        raise result
    code, issued_client_id = result
    return save_exchange(
        client,
        store,
        code=code,
        client_id=issued_client_id,
        redirect_uri=redirect_uri,
        verifier=verifier,
        nonce=nonce,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["login", "status", "models"])
    parser.add_argument("--storage-dir", type=Path, default=Path(".local/chatgpt"))
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=600)
    arguments = parser.parse_args()
    try:
        if not 0 <= arguments.port <= 65_535 or not 1 <= arguments.timeout <= 1_800:
            raise AuthorizationError("invalid_arguments")
        store = PrivateStore(arguments.storage_dir)
        if arguments.command == "login":
            try:
                import jwt  # noqa: F401
            except ImportError:
                raise AuthorizationError("install_oauth_dependency") from None
        with store.lock(), httpx.Client(timeout=30, follow_redirects=False) as client:
            session = store.read("session.json")
            if arguments.command == "login":
                session = login(client, store, arguments.port, arguments.timeout)
                result = safe_status(session)
            elif arguments.command == "models":
                result = {"models": available_models(client, session), "inference_requests": 0}
            else:
                result = safe_status(session)
        print(json.dumps(result), flush=True)
    except AuthorizationError as error:
        print(json.dumps({"error": str(error), "inference_requests": 0}), flush=True)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print(json.dumps({"error": "authorization_cancelled", "inference_requests": 0}), flush=True)
        raise SystemExit(130) from None
    except Exception:
        # Do not print raw network, filesystem, JWT, or provider exceptions.
        print(
            json.dumps({"error": "authorization_preflight_failed", "inference_requests": 0}),
            flush=True,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
