"""Authentication (JWT bearer tokens) and authorization helpers.

Roles
-----
student : may read/pay ONLY their own tuition (token ``sub`` == studentNo)
bank    : banking-app service client; may query/pay for any student
admin   : university web site admin; may add tuition and list unpaid

Passwords are stored as PBKDF2-HMAC-SHA256 hashes (stdlib, 200k iterations).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from typing import Iterable

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

JWT_SECRET = os.environ.get("JWT_SECRET", "dev-only-secret-change-me-32-bytes-min!")
JWT_ALG = "HS256"
JWT_TTL_SECONDS = int(os.environ.get("JWT_TTL_SECONDS", "1800"))
JWT_ISSUER = "tuition-api"
PBKDF2_ITERATIONS = 200_000


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2${PBKDF2_ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iters, salt_b64, dk_b64 = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt_b64), int(iters))
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except (ValueError, TypeError):
        return False


def issue_token(username: str, role: str) -> tuple[str, int]:
    now = int(time.time())
    payload = {"sub": username, "role": role, "iat": now,
               "exp": now + JWT_TTL_SECONDS, "iss": JWT_ISSUER}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG), JWT_TTL_SECONDS


class Principal:
    def __init__(self, username: str, role: str):
        self.username = username
        self.role = role

    def __repr__(self) -> str:
        return f"{self.role}:{self.username}"


_bearer = HTTPBearer(auto_error=False)


def current_principal(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> Principal:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            detail={"code": "UNAUTHENTICATED", "message": "Bearer token required"},
                            headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = jwt.decode(creds.credentials, JWT_SECRET, algorithms=[JWT_ALG], issuer=JWT_ISSUER)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            detail={"code": "TOKEN_EXPIRED", "message": "Token expired"},
                            headers={"WWW-Authenticate": "Bearer"})
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            detail={"code": "INVALID_TOKEN", "message": "Invalid token"},
                            headers={"WWW-Authenticate": "Bearer"})
    return Principal(payload["sub"], payload["role"])


def require_roles(*roles: str):
    def dep(p: Principal = Depends(current_principal)) -> Principal:
        if p.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                detail={"code": "FORBIDDEN", "message": f"Requires role: {', '.join(roles)}"})
        return p
    return dep


def ensure_can_access_student(p: Principal, student_no: str, allowed_roles: Iterable[str] = ("bank",)) -> None:
    """Object-level authorization: a student token can only touch its own account."""
    if p.role in allowed_roles:
        return
    if p.role == "student" and p.username == student_no:
        return
    raise HTTPException(status.HTTP_403_FORBIDDEN,
                        detail={"code": "FORBIDDEN",
                                "message": "Not allowed to access another student's account"})
