"""Better Auth JWT verification helpers."""

from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import Any

import jwt
from jwt import PyJWKClient


@lru_cache(maxsize=4)
def get_jwks_client(jwks_url: str) -> PyJWKClient:
    """Reuse JWKS clients by URL so signing keys stay cached across requests."""
    return PyJWKClient(jwks_url)


def clear_jwks_client_cache() -> None:
    """Reset the shared JWKS client cache, mainly for tests."""
    get_jwks_client.cache_clear()


async def verify_better_auth_jwt(
    token: str,
    *,
    jwks_url: str,
    issuer: str,
    audience: str,
) -> dict[str, Any]:
    """Verify a Better Auth JWT against its remote JWKS."""

    signing_key = await asyncio.to_thread(
        get_jwks_client(jwks_url).get_signing_key_from_jwt,
        token,
    )
    payload = await asyncio.to_thread(
        jwt.decode,
        token,
        signing_key.key,
        algorithms=["EdDSA"],
        issuer=issuer,
        audience=audience,
    )
    return dict(payload)
