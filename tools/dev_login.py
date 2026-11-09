"""Local dev login helper: automated email-OTP sign-in against the local frontend.

The login flow is fully driven over the real HTTP endpoints of the local
frontend (better-auth), so the exercised path is identical to a real user
login. The OTP never needs to reach a real mailbox: better-auth stores it in
plaintext in the local database (`verification.value`, format "<otp>:<attempts>")
the moment it is generated, and this script reads it from there.

Usage:
    conda run -n parlasoul python tools/dev_login.py <email>
    conda run -n parlasoul python tools/dev_login.py <email> --base-url http://localhost:3002

Every run performs the full OTP flow (send code -> read code from DB -> sign in),
which is the same path a real user takes and doubles as a login-pipeline smoke test.

On success a JSON object is printed to stdout:
    {email, base_url, session_token, cookie_header, jwt, user_id}
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_BASE_URL = "http://localhost:3002"
SESSION_COOKIE = "better-auth.session_token"
OTP_WAIT_SECONDS = 10.0
HTTP_TIMEOUT_SECONDS = 15.0


def resolve_base_url(override: str | None) -> str:
    if override:
        return override.rstrip("/")
    jwks_url = os.getenv("BETTER_AUTH_JWKS_URL", "").strip()
    if jwks_url:
        return jwks_url.removesuffix("/api/auth/jwks")
    return DEFAULT_BASE_URL


def resolve_dsn() -> str:
    dsn = os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        raise RuntimeError("DATABASE_URL is missing in backend .env")
    return dsn.replace("postgresql+asyncpg://", "postgresql://", 1)


def normalize_email(email: str) -> str:
    email = email.strip().lower()
    if "@" not in email:
        raise ValueError(f"invalid email: {email!r}")
    return email


def otp_identifier(email: str, otp_type: str = "sign-in") -> str:
    # Mirrors better-auth emailOTP: identifier = "{type}-otp-{email}".
    return f"{otp_type}-otp-{email}"


async def fetch_otp_from_db(email: str) -> str:
    """Read the newest non-expired OTP for `email` from the verification table."""
    deadline = time.monotonic() + OTP_WAIT_SECONDS
    async with asyncpg.create_pool(resolve_dsn(), min_size=1, max_size=1) as pool:
        while True:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    'SELECT value, "expiresAt" FROM verification '
                    "WHERE identifier = $1 "
                    'ORDER BY "createdAt" DESC LIMIT 1',
                    otp_identifier(email),
                )
            if row:
                otp = row["value"].split(":")[0]
                expires_at: datetime = row["expiresAt"]
                if expires_at > datetime.now(UTC):
                    return otp
                raise RuntimeError(
                    f"OTP for {email} expired at {expires_at.isoformat()}; resend first"
                )
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"no verification record found for {email} within {OTP_WAIT_SECONDS:.0f}s"
                )
            await asyncio.sleep(0.5)


def auth_post(client: httpx.Client, path: str, payload: dict) -> httpx.Response:
    # better-auth validates Origin for non-GET requests against trustedOrigins.
    headers = {
        "Origin": str(client.base_url).rstrip("/"),
        "Content-Type": "application/json",
    }
    response = client.post(path, json=payload, headers=headers)
    if response.status_code >= 400:
        raise RuntimeError(f"POST {path} failed with {response.status_code}: {response.text}")
    return response


def request_jwt(base_url: str, session_token: str) -> str:
    with httpx.Client(base_url=base_url, timeout=HTTP_TIMEOUT_SECONDS) as client:
        cookies = {SESSION_COOKIE: session_token}
        response = client.get("/api/auth/token", cookies=cookies)
        if response.status_code >= 400:
            raise RuntimeError(
                f"GET /api/auth/token failed with {response.status_code}: {response.text}"
            )
        token = response.json().get("token")
        if not token:
            raise RuntimeError(f"/api/auth/token returned no token: {response.text}")
        return token


def login_via_otp_flow(base_url: str, email: str) -> str:
    """Run the real OTP login flow over HTTP and return the session token."""
    with httpx.Client(base_url=base_url, timeout=HTTP_TIMEOUT_SECONDS) as client:
        auth_post(
            client,
            "/api/auth/email-otp/send-verification-otp",
            {"email": email, "type": "sign-in"},
        )
        otp = asyncio.run(fetch_otp_from_db(email))
        auth_post(client, "/api/auth/sign-in/email-otp", {"email": email, "otp": otp})
        session_token = client.cookies.get(SESSION_COOKIE)
        if not session_token:
            raise RuntimeError(f"{SESSION_COOKIE} not found in sign-in response cookies")
    return session_token


def jwt_subject(token: str) -> str | None:
    try:
        payload_b64 = token.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)))
        subject = payload.get("sub")
        return str(subject) if subject is not None else None
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Automated local email-OTP login for testing.")
    parser.add_argument("email", help="account email (new emails are auto-registered)")
    parser.add_argument(
        "--base-url",
        default=None,
        help=f"frontend base URL (default: {DEFAULT_BASE_URL} or BETTER_AUTH_JWKS_URL)",
    )
    args = parser.parse_args()

    base_url = resolve_base_url(args.base_url)
    email = normalize_email(args.email)
    session_token = login_via_otp_flow(base_url, email)

    jwt = request_jwt(base_url, session_token)
    print(
        json.dumps(
            {
                "email": email,
                "base_url": base_url,
                "session_token": session_token,
                "cookie_header": f"{SESSION_COOKIE}={session_token}",
                "jwt": jwt,
                "user_id": jwt_subject(jwt),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
