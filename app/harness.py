import re
from dataclasses import asdict, dataclass
from typing import Optional
from urllib.parse import unquote, urlparse

import httpx

from app.config import settings


_PATH_PATTERNS = (
    re.compile(
        r"^/ng/account/(?P<account>[^/]+)/cd/orgs/(?P<org>[^/]+)/projects/(?P<project>[^/]+)/"
        r"pipelines/(?P<pipeline>[^/]+)/executions/(?P<execution>[^/]+)(?:/.*)?/?$"
    ),
    re.compile(
        r"^/ng/account/(?P<account>[^/]+)/ci/orgs/(?P<org>[^/]+)/projects/(?P<project>[^/]+)/"
        r"pipelines/(?P<pipeline>[^/]+)/executions/(?P<execution>[^/]+)(?:/.*)?/?$"
    ),
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:@-]{1,200}$")


@dataclass(frozen=True)
class ExecutionRef:
    account_id: str
    org_id: str
    project_id: str
    pipeline_id: str
    execution_id: str

    def as_dict(self):
        return asdict(self)


def parse_execution_url(value: str) -> ExecutionRef:
    if len(value) > 2048:
        raise ValueError("Execution URL is too long.")
    parsed = urlparse(value.strip())
    base = urlparse(settings.harness_base_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.hostname.lower() != base.hostname.lower()
        or parsed.port != base.port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"Enter a Harness execution URL on {base.hostname}.")

    path = unquote(parsed.path)
    match = next((pattern.fullmatch(path) for pattern in _PATH_PATTERNS if pattern.fullmatch(path)), None)
    if match is None:
        raise ValueError("URL must identify a Harness CD or CI pipeline execution.")
    values = match.groupdict()
    if any(not _IDENTIFIER.fullmatch(value) for value in values.values()):
        raise ValueError("Execution URL contains an invalid Harness identifier.")
    return ExecutionRef(
        account_id=values["account"],
        org_id=values["org"],
        project_id=values["project"],
        pipeline_id=values["pipeline"],
        execution_id=values["execution"],
    )


class HarnessError(RuntimeError):
    pass


class HarnessClient:
    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None):
        self.base_url = (base_url or settings.harness_base_url).rstrip("/")
        self.api_key = api_key or settings.harness_api_key
        origin = urlparse(settings.harness_base_url)
        configured = urlparse(self.base_url)
        if (
            configured.scheme != "https"
            or configured.hostname != origin.hostname
            or configured.port != origin.port
            or configured.path
            or configured.query
            or configured.fragment
        ):
            raise ValueError("Harness API base URL must use the configured Harness host over HTTPS.")
        if not self.api_key:
            raise HarnessError("Harness API key is not configured.")

    async def _request(self, method: str, path: str, *, params: dict, json: Optional[dict] = None):
        headers = {"x-api-key": self.api_key, "Accept": "application/json"}
        last_error = None
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
                    response = await client.request(
                        method, f"{self.base_url}{path}", params=params, json=json, headers=headers
                    )
                if response.status_code in (429, 502, 503, 504) and attempt == 0:
                    continue
                if response.status_code >= 400:
                    raise HarnessError(f"Harness API returned HTTP {response.status_code}.")
                try:
                    return response.json()
                except ValueError as exc:
                    raise HarnessError("Harness API returned an invalid JSON response.") from exc
            except httpx.TimeoutException as exc:
                last_error = exc
                if attempt:
                    break
            except httpx.RequestError as exc:
                last_error = exc
                if attempt:
                    break
        raise HarnessError("Harness API request failed after retrying.") from last_error

    @staticmethod
    def _params(ref: ExecutionRef):
        return {
            "accountIdentifier": ref.account_id,
            "orgIdentifier": ref.org_id,
            "projectIdentifier": ref.project_id,
        }

    async def get_execution(self, ref: ExecutionRef):
        return await self._request(
            "GET",
            f"/pipeline/api/pipelines/execution/v2/{ref.execution_id}",
            params=self._params(ref),
        )

    async def submit_decision(self, ref: ExecutionRef, action: str, comment: str):
        if action not in ("approve", "reject"):
            raise ValueError("Action must be approve or reject.")
        status = "APPROVED" if action == "approve" else "REJECTED"
        return await self._request(
            "POST",
            f"/pipeline/api/pipeline/execution/{ref.execution_id}/approval",
            params=self._params(ref),
            json={"status": status, "comments": comment},
        )


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def summarize_execution(payload: dict, ref: ExecutionRef) -> dict:
    nodes = list(_walk(payload))
    approval = next(
        (
            node for node in nodes
            if str(node.get("nodeType", node.get("type", ""))).upper() in {"APPROVAL", "APPROVAL_STAGE"}
            or "approval" in str(node.get("stepType", "")).lower()
        ),
        None,
    )
    pending = approval is not None and str(
        approval.get("status", approval.get("nodeStatus", ""))
    ).upper() in {"", "RUNNING", "WAITING", "PAUSED", "IN_PROGRESS", "WAITING_APPROVAL", "APPROVAL_WAITING"}
    environment = next(
        (
            str(node.get(key))
            for node in nodes
            for key in ("environmentName", "environment")
            if isinstance(node.get(key), str) and node.get(key)
        ),
        "Unknown",
    )
    if environment.lower() in {"prod", "production"}:
        environment = "Production"
    elif environment.lower() == "uat":
        environment = "UAT"
    pipeline_name = next(
        (str(node.get(k)) for node in nodes for k in ("pipelineName", "name") if node.get(k)),
        ref.pipeline_id,
    )
    triggered_by = next(
        (
            str(node.get(k))
            for node in nodes
            for k in ("triggeredBy", "triggeredByName", "userName")
            if isinstance(node.get(k), (str, int)) and node.get(k)
        ),
        "Unknown",
    )
    status = next(
        (str(node.get(k)) for node in nodes for k in ("status", "pipelineStatus") if node.get(k)),
        "Unknown",
    )
    return {
        **ref.as_dict(),
        "pipeline_name": pipeline_name,
        "environment": environment,
        "status": status,
        "triggered_by": triggered_by,
        "approval_pending": pending,
        "approval_name": (
            str(approval.get("name", approval.get("identifier", "Approval"))) if approval else None
        ),
        "approval_message": (
            str(approval.get("approvalMessage", approval.get("message", ""))) if approval else ""
        ),
        "authorized_approvers": (
            approval.get("approvers", approval.get("approverUsers", [])) if approval else []
        ),
        "artifact": next(
            (str(node.get(k)) for node in nodes for k in ("artifactVersion", "tag", "imagePath") if node.get(k)),
            "Unknown",
        ),
        "service": next(
            (str(node.get(k)) for node in nodes for k in ("serviceName", "serviceIdentifier") if node.get(k)),
            "Unknown",
        ),
    }
