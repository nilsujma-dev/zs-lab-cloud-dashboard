"""Single-password login with a signed, HttpOnly session cookie (12h)."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets

from fastapi import HTTPException, Request, Response
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

COOKIE_NAME = "switchboard_session"
SESSION_TTL_S = 12 * 60 * 60


class Auth:
    def __init__(self, password: str | None = None, secret_key: str | None = None) -> None:
        self._password = password if password is not None else os.environ.get("SWITCHBOARD_PASSWORD", "")
        key = secret_key if secret_key is not None else os.environ.get("SWITCHBOARD_SECRET_KEY", "")
        if not key:
            raise RuntimeError("SWITCHBOARD_SECRET_KEY is not set")
        # Separate signing key derived from the Fernet key so the two never share bytes.
        signing_key = hashlib.sha256(b"switchboard-session:" + key.encode()).digest()
        self._signer = TimestampSigner(signing_key)

    @property
    def enabled(self) -> bool:
        return bool(self._password)

    def check_password(self, candidate: str) -> bool:
        """Constant-time compare; an unset SWITCHBOARD_PASSWORD means nobody can log in."""
        if not self._password:
            return False
        return hmac.compare_digest(candidate.encode("utf-8"), self._password.encode("utf-8"))

    def issue_cookie(self, response: Response) -> None:
        token = self._signer.sign(secrets.token_urlsafe(24)).decode()
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=SESSION_TTL_S,
            httponly=True,
            samesite="lax",
            path="/",
        )

    def clear_cookie(self, response: Response) -> None:
        response.delete_cookie(COOKIE_NAME, path="/")

    def is_authenticated(self, request: Request) -> bool:
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            return False
        try:
            self._signer.unsign(token, max_age=SESSION_TTL_S)
        except (BadSignature, SignatureExpired):
            return False
        return True

    def require(self, request: Request) -> None:
        """FastAPI dependency: 401 in the spec's error shape when the session cookie is missing/invalid."""
        if not self.is_authenticated(request):
            raise HTTPException(
                status_code=401,
                detail={"error": "Not authenticated", "code": "unauthenticated"},
            )
