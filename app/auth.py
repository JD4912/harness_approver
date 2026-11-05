from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException, Request

from app.config import settings


@dataclass(frozen=True)
class User:
    email: str
    name: str
    roles: frozenset[str]


def current_user(request: Request) -> User:
    identity = request.session.get("user")
    if not identity:
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    return User(
        email=identity["email"],
        name=identity.get("name") or identity["email"],
        roles=frozenset(identity.get("roles", [])),
    )


def require_role(user: User, *roles: str):
    if not user.roles.intersection(roles):
        raise HTTPException(status_code=403, detail="Your account is not authorized for this action.")
    return user


def require_csrf(request: Request, supplied_token: Optional[str]):
    expected = request.session.get("csrf_token")
    if not expected or not supplied_token or supplied_token != expected:
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token.")


def roles_from_claims(claims: dict) -> frozenset[str]:
    groups = claims.get(settings.oidc_roles_claim, [])
    if isinstance(groups, str):
        groups = [groups]
    mapped = {
        settings.oidc_role_map[group]
        for group in groups
        if group in settings.oidc_role_map
    }
    return frozenset(mapped.intersection({
        "requester", "uat_approver", "prod_approver", "admin", "auditor"
    }))
