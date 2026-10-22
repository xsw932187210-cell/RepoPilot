from __future__ import annotations

import base64
import hashlib
import json
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from repopilot import chatgpt_auth as auth


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65_537, key_size=2048)


def signed_token(key, **claims):
    payload = {
        "iss": auth.ISSUER,
        "aud": "oaiapp_test",
        "sub": "test-subject",
        "exp": int(time.time()) + 3600,
        "iat": int(time.time()),
        "nonce": "test-nonce",
        **claims,
    }
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": "test-key"})


def mock_provider(key, tokens=None):
    requests = []
    public_key = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    public_key["kid"] = "test-key"

    def respond(request):
        requests.append(request)
        if str(request.url) == auth.JWKS:
            return httpx.Response(200, json={"keys": [public_key]})
        if str(request.url) == auth.TOKEN:
            assert request.method == "POST"
            return httpx.Response(200, json=tokens)
        raise AssertionError("unexpected network route")

    return httpx.Client(transport=httpx.MockTransport(respond)), requests


def exchange(store, client):
    return auth.save_exchange(
        client,
        store,
        code="synthetic-code",
        client_id="oaiapp_test",
        redirect_uri="http://127.0.0.1:1234/auth/callback",
        verifier="synthetic-verifier",
        nonce="test-nonce",
    )


def test_private_storage_is_atomic_private_and_host_is_stable(tmp_path):
    store = auth.PrivateStore(tmp_path / "private")
    host_id = store.host_id()
    with store.lock():
        store.write("session.json", {"access_token": "synthetic-first"})
        store.write("session.json", {"access_token": "synthetic-second"})
    assert auth.PrivateStore(store.root).host_id() == host_id
    assert store.read("session.json") == {"access_token": "synthetic-second"}
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in store.root.iterdir())
    assert not list(store.root.glob(".pending-*"))


def test_private_store_rejects_symlinks_and_public_credentials(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(auth.AuthorizationError, match="unsafe_storage_path"):
        auth.PrivateStore(link / "child")
    store = auth.PrivateStore(real)
    file = real / "session.json"
    file.write_text('{"access_token":"synthetic"}')
    file.chmod(0o644)
    with pytest.raises(auth.AuthorizationError, match="unsafe_storage_permissions"):
        store.read("session.json")
    file.unlink()
    file.symlink_to(tmp_path / "outside")
    with pytest.raises(OSError):
        store.read("session.json")


def test_sessions_cannot_be_modified_concurrently(tmp_path):
    store = auth.PrivateStore(tmp_path / "private")
    with store.lock(), pytest.raises(auth.AuthorizationError, match="authorization_busy"):
        with auth.PrivateStore(store.root).lock():
            pytest.fail("must not obtain another session lock")


def test_new_and_returning_authorization_use_pkce_and_no_token_hint():
    first = auth.authorization_url("dynamic_agent_client", "urn:uuid:test", "http://127.0.0.1:1/auth/callback")
    url, state, nonce, verifier = first
    parameters = parse_qs(urlsplit(url).query)
    assert parameters["agent_name_hint"] == ["RepoPilot"]
    assert parameters["state"] == [state]
    assert parameters["nonce"] == [nonce]
    assert parameters["scope"] == [auth.SCOPES]
    assert parameters["code_challenge"] == [
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    ]
    again = auth.authorization_url("oaiapp_test", "urn:uuid:test", "http://127.0.0.1:2/auth/callback")
    next_parameters = parse_qs(urlsplit(again[0]).query)
    assert "agent_name_hint" not in next_parameters
    assert "id_token_hint" not in next_parameters
    assert all(left != right for left, right in zip(first[1:], again[1:], strict=True))


@pytest.mark.parametrize("query,code", [
    ("state=wrong&code=secret&client_id=oaiapp_test", "invalid_callback_state"),
    ("state=中文&code=secret", "invalid_callback_state"),
    ("state=ok&state=ok&code=secret", "invalid_callback"),
    ("state=ok&error=access_denied", "authorization_declined"),
    ("state=ok&code=secret", "registration_incomplete"),
    ("state=ok&code=secret&client_id=dynamic_agent_client", "registration_incomplete"),
    ("state=ok&client_id=oaiapp_test", "authorization_code_missing"),
])
def test_invalid_callbacks_never_return_an_authorization_code(query, code):
    with pytest.raises(auth.AuthorizationError, match=f"^{code}$"):
        auth.validate_callback(f"/auth/callback?{query}", "ok", "dynamic_agent_client")


def test_callback_registration_is_bound_and_returning_client_can_be_omitted():
    assert auth.validate_callback(
        "/auth/callback?state=ok&code=synthetic&client_id=oaiapp_test", "ok", "dynamic_agent_client"
    ) == ("synthetic", "oaiapp_test")
    assert auth.validate_callback(
        "/auth/callback?state=ok&code=synthetic", "ok", "oaiapp_test"
    ) == ("synthetic", "oaiapp_test")
    with pytest.raises(auth.AuthorizationError, match="registration_mismatch"):
        auth.validate_callback(
            "/auth/callback?state=ok&code=synthetic&client_id=other", "ok", "oaiapp_test"
        )


@pytest.mark.parametrize("claims", [
    {"iss": "https://other.example"},
    {"aud": "another-app"},
    {"nonce": "another-attempt"},
    {"nonce": "中文"},
    {"exp": int(time.time()) - 60},
    {"sub": ""},
])
def test_signed_identity_must_match_issuer_audience_nonce_and_expiry(signing_key, claims):
    client, _requests = mock_provider(signing_key)
    with client, pytest.raises(auth.AuthorizationError, match="invalid_identity"):
        auth.verify_identity(
            client, signed_token(signing_key, **claims), "oaiapp_test", "test-nonce"
        )


def test_invalid_signature_and_unsigned_jwt_are_rejected(signing_key):
    other_key = rsa.generate_private_key(public_exponent=65_537, key_size=2048)
    client, _requests = mock_provider(signing_key)
    with client:
        for token in (signed_token(other_key), jwt.encode({"sub": "forged"}, "", algorithm="none")):
            with pytest.raises(auth.AuthorizationError, match="invalid_identity"):
                auth.verify_identity(client, token, "oaiapp_test", "test-nonce")


def test_exchange_checks_granted_scope_not_callback_and_saves_only_verified_identity(
    tmp_path, signing_key
):
    store = auth.PrivateStore(tmp_path / "private")
    client, requests = mock_provider(signing_key, {
        "access_token": "synthetic-access",
        "refresh_token": "synthetic-refresh",
        "id_token": signed_token(signing_key),
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "openid profile email",
    })
    with client:
        session = exchange(store, client)
    assert not auth.safe_status(session)["plan_usage_authorized"]
    status = json.dumps(auth.safe_status(session))
    assert "synthetic" not in status and "test-subject" not in status
    assert store.read("session.json")["refresh_token"] == "synthetic-refresh"  # noqa: S105
    assert store.read("registration.json")["client_id"] == "oaiapp_test"
    posted = parse_qs(requests[0].content.decode())
    assert posted["client_id"] == ["oaiapp_test"]
    assert posted["resource"] == [auth.RESOURCE]
    assert posted["redirect_uri"] == ["http://127.0.0.1:1234/auth/callback"]
    assert posted["code_verifier"] == ["synthetic-verifier"]
    assert "client_secret" not in posted
    assert {request.method for request in requests} == {"GET", "POST"}
    assert not any(request.url.path.endswith("responses") for request in requests)


def test_failed_reauthorization_preserves_existing_credentials(tmp_path, signing_key):
    store = auth.PrivateStore(tmp_path / "private")
    original = {"subject": "original", "client_id": "oaiapp_test", "access_token": "keep-private"}
    store.write("session.json", original)
    client, _requests = mock_provider(signing_key, {
        "id_token": signed_token(signing_key, sub="different-account"),
        "access_token": "do-not-use",
        "expires_in": 3600,
        "token_type": "Bearer",
        "scope": auth.SCOPES,
    })
    with client, pytest.raises(auth.AuthorizationError, match="account_mismatch"):
        exchange(store, client)
    assert store.read("session.json") == original


def test_model_catalog_requires_current_plan_permission_and_never_runs_inference():
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == "GET" and str(request.url) == f"{auth.RESOURCE}/models"
        assert request.headers["Authorization"] == "Bearer synthetic"
        return httpx.Response(200, json={"models": [
            {
                "slug": "visible-model", "display_name": "Visible",
                "visibility": "list", "private": "omit",
            },
            {"slug": "hidden-model", "display_name": "Hidden", "visibility": "hide"},
        ]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(auth.AuthorizationError, match="plan_usage_not_authorized"):
            auth.available_models(client, {})
        with pytest.raises(auth.AuthorizationError, match="sign_in_again_to_renew"):
            auth.available_models(client, {"scopes": [auth.PLAN_SCOPE], "expires_at": 0})
        assert requests == []
        models = auth.available_models(client, {
            "scopes": [auth.PLAN_SCOPE], "expires_at": time.time() + 3600,
            "access_token": "synthetic",
        })
    assert models == [{"slug": "visible-model", "display_name": "Visible"}]


def test_provider_errors_do_not_echo_sensitive_body():
    with httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(
        403, json={"detail": "synthetic-access-token account@example.com"}
    ))) as client, pytest.raises(auth.AuthorizationError, match="^provider_http_403$"):
        auth.http_json(client, "GET", f"{auth.RESOURCE}/models")


def test_actual_loopback_rejects_wrong_host_and_state_then_accepts_once(
    tmp_path, signing_key, monkeypatch, capsys
):
    prepared = Queue()
    original_url = auth.authorization_url

    def capture(*args):
        result = original_url(*args)
        prepared.put(result)
        return result

    monkeypatch.setattr(auth, "authorization_url", capture)
    store = auth.PrivateStore(tmp_path / "private")
    public_key = jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key(), as_dict=True)
    public_key["kid"] = "test-key"
    nonce = ""

    def respond(request):
        if str(request.url) == auth.JWKS:
            return httpx.Response(200, json={"keys": [public_key]})
        assert str(request.url) == auth.TOKEN
        return httpx.Response(200, json={
            "id_token": signed_token(signing_key, nonce=nonce),
            "access_token": "synthetic-loopback-access",
            "token_type": "Bearer", "expires_in": 3600, "scope": auth.SCOPES,
        })

    with httpx.Client(transport=httpx.MockTransport(respond)) as provider:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(auth.login, provider, store, 0, 10)
            url, state, nonce, _verifier = prepared.get(timeout=5)
            redirect = parse_qs(urlsplit(url).query)["redirect_uri"][0]
            origin = redirect.removesuffix(auth.CALLBACK)
            with httpx.Client(trust_env=False, timeout=5, follow_redirects=False) as browser:
                assert browser.get(f"{origin}/").status_code == 200
                start = browser.get(f"{origin}/start")
                assert start.status_code == 302 and start.headers["location"] == url
                wrong_host = browser.get(f"{origin}/", headers={"Host": "evil.example"})
                assert wrong_host.status_code == 400
                assert browser.get(f"{redirect}?state=wrong&code=secret").status_code == 400
                callback = browser.get(f"{redirect}?{urlencode({
                    'state': state, 'code': 'synthetic-callback-code', 'client_id': 'oaiapp_test',
                })}")
                assert callback.status_code == 200
                assert "synthetic-callback-code" not in callback.text
                assert callback.headers["referrer-policy"] == "no-referrer"
                assert callback.headers["cache-control"] == "no-store"
            result = future.result(timeout=5)
    assert auth.safe_status(result)["plan_usage_authorized"]
    output = capsys.readouterr()
    assert "synthetic" not in output.out + output.err
    assert "continue_with_chatgpt" in output.out
