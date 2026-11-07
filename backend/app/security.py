"""API authentication and roles.

Three roles, each with its own token (see Settings):
    viewer    read-only
    agent     read, edit firmware sources, build, propose deployments - but never approve them
    operator  everything, including approving deployments and recoveries

Send the token as   Authorization: Bearer <token>
If no token has been configured at all, every protected endpoint refuses (fail closed).
"""
from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .errors import ApiError

ROLE_RANK = {"viewer": 1, "agent": 2, "operator": 3}

_bearer = HTTPBearer(auto_error=False, description="API token for your role (viewer / agent / operator)")


@dataclass(frozen=True)
class Principal:
    name: str   # same as the role: there is one token per role
    role: str

    def at_least(self, role: str) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK[role]


def new_token() -> str:
    return secrets.token_urlsafe(24)


def principal_for(tokens: dict, presented: str) -> Optional[Principal]:
    """Constant-time comparison against every configured token."""
    found: Optional[Principal] = None
    for role, tok in tokens.items():
        if hmac.compare_digest(presented.encode("utf-8"), tok.encode("utf-8")):
            found = Principal(name=role, role=role)
    return found


def authenticate(request: Request, creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)) -> Principal:
    tokens = request.app.state.settings.role_tokens
    if not tokens:
        raise ApiError(503, "auth_not_configured",
                       "no API tokens are configured; run:  python -m app.cli make-tokens")
    if creds is None or creds.scheme.lower() != "bearer" or not creds.credentials:
        raise ApiError(401, "unauthorized", "missing API token (send  Authorization: Bearer <token>)")
    p = principal_for(tokens, creds.credentials)
    if p is None:
        raise ApiError(401, "unauthorized", "the API token is not valid")
    request.state.principal = p
    return p


def require(min_role: str):
    """Dependency factory:  Depends(require('operator'))"""
    def dep(request: Request, principal: Principal = Depends(authenticate)) -> Principal:
        if not principal.at_least(min_role):
            raise ApiError(403, "forbidden", f"this needs the '{min_role}' role; your token is '{principal.role}'")
        return principal
    return dep