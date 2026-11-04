from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.core.jwks import clear_jwks_client_cache, verify_better_auth_jwt


class _SigningKey:
    def __init__(self, key) -> None:
        self.key = key


class _StubJwksClient:
    def __init__(self, key) -> None:
        self._key = key

    def get_signing_key_from_jwt(self, token: str) -> _SigningKey:
        return _SigningKey(self._key)


def _create_token(
    private_key: Ed25519PrivateKey,
    *,
    subject: str = "0d1c5303-20dd-4eb9-a68b-e7d30610f1c4",
    issuer: str = "http://localhost:3001",
    audience: str = "http://localhost:3001",
    expires_delta: timedelta = timedelta(minutes=5),
) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": subject,
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "exp": now + expires_delta,
    }
    headers = {
        "alg": "EdDSA",
        "kid": base64.urlsafe_b64encode(b"test-kid").decode().rstrip("="),
    }
    return jwt.encode(payload, private_key, algorithm="EdDSA", headers=headers)


@pytest.mark.asyncio
async def test_verify_better_auth_jwt_accepts_valid_token(monkeypatch) -> None:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    token = _create_token(private_key)

    clear_jwks_client_cache()
    monkeypatch.setattr(
        "src.core.jwks.get_jwks_client",
        lambda jwks_url: _StubJwksClient(public_key),
    )

    payload = await verify_better_auth_jwt(
        token,
        jwks_url="http://localhost:3001/api/auth/jwks",
        issuer="http://localhost:3001",
        audience="http://localhost:3001",
    )

    assert payload["sub"] == "0d1c5303-20dd-4eb9-a68b-e7d30610f1c4"


@pytest.mark.asyncio
async def test_verify_better_auth_jwt_rejects_wrong_audience(monkeypatch) -> None:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    token = _create_token(private_key, audience="http://localhost:9999")

    clear_jwks_client_cache()
    monkeypatch.setattr(
        "src.core.jwks.get_jwks_client",
        lambda jwks_url: _StubJwksClient(public_key),
    )

    with pytest.raises(jwt.InvalidAudienceError):
        await verify_better_auth_jwt(
            token,
            jwks_url="http://localhost:3001/api/auth/jwks",
            issuer="http://localhost:3001",
            audience="http://localhost:3001",
        )


@pytest.mark.asyncio
async def test_verify_better_auth_jwt_rejects_wrong_issuer(monkeypatch) -> None:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    token = _create_token(private_key, issuer="http://localhost:9999")

    clear_jwks_client_cache()
    monkeypatch.setattr(
        "src.core.jwks.get_jwks_client",
        lambda jwks_url: _StubJwksClient(public_key),
    )

    with pytest.raises(jwt.InvalidIssuerError):
        await verify_better_auth_jwt(
            token,
            jwks_url="http://localhost:3001/api/auth/jwks",
            issuer="http://localhost:3001",
            audience="http://localhost:3001",
        )


@pytest.mark.asyncio
async def test_verify_better_auth_jwt_rejects_expired_token(monkeypatch) -> None:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    token = _create_token(private_key, expires_delta=timedelta(minutes=-1))

    clear_jwks_client_cache()
    monkeypatch.setattr(
        "src.core.jwks.get_jwks_client",
        lambda jwks_url: _StubJwksClient(public_key),
    )

    with pytest.raises(jwt.ExpiredSignatureError):
        await verify_better_auth_jwt(
            token,
            jwks_url="http://localhost:3001/api/auth/jwks",
            issuer="http://localhost:3001",
            audience="http://localhost:3001",
        )
