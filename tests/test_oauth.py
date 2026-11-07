import asyncio
import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from gateway import oauth
from gateway.errors import GatewayError
from gateway.storage import Storage


def record():
    return {
        "client_id": "registered-test",
        "subject": "subject-test",
        "email": "test@example.invalid",
        "access_token": "access-test",
        "refresh_token": "refresh-test",
        "expires_at": 0,
        "scopes": ["chatgpt.tokens.use.direct", "resource.invoke"],
    }


def test_id_token_signature_nonce_audience_and_expiration(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk["kid"] = "test-key"
    monkeypatch.setattr(oauth, "oidc_metadata", lambda: {"jwks_uri": "https://auth.openai.com/test"})
    real_client = httpx.Client
    monkeypatch.setattr(
        oauth.httpx,
        "Client",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"keys": [jwk]}))
        ),
    )
    claims = {
        "iss": oauth.ISSUER,
        "aud": "registered-test",
        "sub": "subject-test",
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
        "nonce": "test-nonce",
    }
    signed = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-key"})
    assert oauth.validate_id_token(signed, "registered-test", "test-nonce")["sub"] == "subject-test"
    with pytest.raises(ValueError):
        oauth.validate_id_token(signed, "registered-test", "wrong-nonce")
    with pytest.raises(jwt.InvalidAudienceError):
        oauth.validate_id_token(signed, "other-registration", "test-nonce")
    expired = jwt.encode(
        {**claims, "exp": int(time.time()) - 100}, key, algorithm="RS256", headers={"kid": "test-key"}
    )
    with pytest.raises(jwt.ExpiredSignatureError):
        oauth.validate_id_token(expired, "registered-test", "test-nonce")
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode(claims, other_key, algorithm="RS256", headers={"kid": "test-key"})
    with pytest.raises(jwt.InvalidSignatureError):
        oauth.validate_id_token(forged, "registered-test", "test-nonce")


def install_refresh(monkeypatch, store, status, body):
    current = record()
    store.write_secrets({"active": current["client_id"], "accounts": {current["client_id"]: current}})
    calls = []
    real_client = httpx.Client

    def handler(request):
        calls.append(request)
        assert b"client_id=registered-test" in request.content
        assert b"scope=" not in request.content
        return httpx.Response(status, json=body)

    monkeypatch.setattr(
        oauth.httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handler))
    )
    return calls


async def test_refresh_serialized_and_rotated(tmp_path, monkeypatch):
    store = Storage(tmp_path)
    calls = install_refresh(
        monkeypatch,
        store,
        200,
        {
            "access_token": "replacement-test",
            "refresh_token": "rotated-test",
            "expires_in": 3600,
            "token_type": "Bearer",
        },
    )
    session = oauth.OAuthSession(store)
    result = await asyncio.gather(session.token(), session.token(), session.token())
    assert result == ["replacement-test"] * 3
    assert len(calls) == 1
    assert store.read_secrets()["accounts"]["registered-test"]["refresh_token"] == "rotated-test"


@pytest.mark.parametrize(
    "code,terminal", [("invalid_grant", True), ("invalid_refresh_token", True), ("invalid_client", False)]
)
async def test_refresh_errors_keep_registration(tmp_path, monkeypatch, code, terminal):
    store = Storage(tmp_path)
    install_refresh(monkeypatch, store, 400, {"error": code})
    with pytest.raises(GatewayError) as caught:
        await oauth.OAuthSession(store).token()
    saved = store.read_secrets()["accounts"]["registered-test"]
    assert saved["subject"] == "subject-test"
    assert ("access_token" not in saved) is terminal
    assert caught.value.status == (401 if terminal else 503)


async def test_refresh_respects_earliest_time(tmp_path, monkeypatch):
    store = Storage(tmp_path)
    calls = install_refresh(monkeypatch, store, 500, {})
    state = store.read_secrets()
    state["accounts"]["registered-test"].update(
        expires_at=time.time() + 10, earliest_refresh_at=time.time() + 5
    )
    store.write_secrets(state)
    assert await oauth.OAuthSession(store).token() == "access-test"
    assert calls == []


def test_account_identity_cannot_change(monkeypatch):
    monkeypatch.setattr(oauth, "validate_id_token", lambda *_: {"sub": "other-account"})
    with pytest.raises(ValueError, match="Account identity changed"):
        oauth.token_record({"id_token": "id-test"}, "registered-test", record())
