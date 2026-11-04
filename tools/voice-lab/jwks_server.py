"""Local JWKS + JWT minter for end-to-end realtime voice tests.

The backend verifies Better Auth JWTs against a remote JWKS (EdDSA).  Running the
real frontend just to obtain a token is unnecessary when the goal is to exercise
the realtime runtime: this utility exposes a local JWKS document and signs
short-lived tokens for an existing local user.

Usage:
    python tools/voice-lab/jwks_server.py --port 8899
    python tools/voice-lab/jwks_server.py --mint <user-uuid>     # print one token

The signing key is persisted under tmp/voice-lab/ so tokens survive restarts.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[2]
KEY_PATH = ROOT / "tmp" / "voice-lab" / "jwks_ed25519.pem"
KID = "voice-lab"
ISSUER = os.environ.get("VOICE_LAB_JWT_ISSUER", "http://localhost:3001")
AUDIENCE = os.environ.get("VOICE_LAB_JWT_AUDIENCE", "http://localhost:3001")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def load_or_create_key() -> Ed25519PrivateKey:
    if KEY_PATH.exists():
        return serialization.load_pem_private_key(KEY_PATH.read_bytes(), password=None)
    KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    KEY_PATH.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return key


def jwks_document(key: Ed25519PrivateKey) -> dict:
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return {
        "keys": [
            {
                "kty": "OKP",
                "crv": "Ed25519",
                "x": _b64url(public),
                "kid": KID,
                "alg": "EdDSA",
                "use": "sig",
            }
        ]
    }


def mint_token(key: Ed25519PrivateKey, user_id: str, *, ttl_seconds: int = 3600) -> str:
    now = int(time.time())
    payload = {
        "sub": user_id,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + ttl_seconds,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(
        payload,
        key,
        algorithm="EdDSA",
        headers={"kid": KID},
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--mint", help="print a token for this user id and exit")
    parser.add_argument("--ttl", type=int, default=12 * 3600)
    args = parser.parse_args()

    key = load_or_create_key()
    if args.mint:
        print(mint_token(key, args.mint, ttl_seconds=args.ttl))
        return

    document = jwks_document(key)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.split("?")[0] in {"/api/auth/jwks", "/jwks"}:
                body = json.dumps(document).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, fmt: str, *values) -> None:  # noqa: A003
            print("[jwks]", fmt % values, flush=True)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"JWKS listening on http://127.0.0.1:{args.port}/api/auth/jwks", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
