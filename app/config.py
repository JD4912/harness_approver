import os
from dataclasses import dataclass
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    app_env: str
    app_secret_key: str
    database_url: str
    harness_base_url: str
    harness_api_key: str
    dev_user_email: str
    dev_user_name: str
    dev_user_roles: frozenset[str]
    oidc_issuer: str
    oidc_client_id: str
    oidc_client_secret: str
    oidc_redirect_uri: str
    oidc_roles_claim: str
    oidc_role_map: dict[str, str]
    session_https_only: bool

    @classmethod
    def from_env(cls) -> "Settings":
        role_map: dict[str, str] = {}
        for item in os.getenv("OIDC_ROLE_MAP", "").split(","):
            if "=" in item:
                group, role = item.split("=", 1)
                role_map[group.strip()] = role.strip()

        settings = cls(
            app_env=os.getenv("APP_ENV", "development"),
            app_secret_key=os.getenv("APP_SECRET_KEY", ""),
            database_url=os.getenv("DATABASE_URL", "sqlite:///./approvals.db"),
            harness_base_url=os.getenv("HARNESS_BASE_URL", "https://app.harness.io").rstrip("/"),
            harness_api_key=os.getenv("HARNESS_API_KEY", ""),
            dev_user_email=os.getenv("DEV_USER_EMAIL", "developer@example.com"),
            dev_user_name=os.getenv("DEV_USER_NAME", "Local Developer"),
            dev_user_roles=frozenset(
                role.strip() for role in os.getenv("DEV_USER_ROLES", "requester,uat_approver,auditor").split(",")
                if role.strip()
            ),
            oidc_issuer=os.getenv("OIDC_ISSUER", "").rstrip("/"),
            oidc_client_id=os.getenv("OIDC_CLIENT_ID", ""),
            oidc_client_secret=os.getenv("OIDC_CLIENT_SECRET", ""),
            oidc_redirect_uri=os.getenv("OIDC_REDIRECT_URI", ""),
            oidc_roles_claim=os.getenv("OIDC_ROLES_CLAIM", "groups"),
            oidc_role_map=role_map,
            session_https_only=os.getenv("SESSION_HTTPS_ONLY", "false").lower() == "true",
        )
        base = urlparse(settings.harness_base_url)
        if (
            base.scheme != "https"
            or not base.hostname
            or base.username
            or base.password
            or base.path not in ("", "/")
            or base.query
            or base.fragment
        ):
            raise ValueError("HARNESS_BASE_URL must be an HTTPS origin without credentials.")
        if settings.app_env != "development":
            if len(settings.app_secret_key) < 32:
                raise ValueError("APP_SECRET_KEY must contain at least 32 characters outside development.")
            if not all((settings.oidc_issuer, settings.oidc_client_id, settings.oidc_client_secret, settings.oidc_redirect_uri)):
                raise ValueError("OIDC issuer, client ID, client secret, and redirect URI are required outside development.")
            if not settings.session_https_only:
                raise ValueError("SESSION_HTTPS_ONLY must be true outside development.")
        return settings


settings = Settings.from_env()
