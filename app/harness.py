import re
from dataclasses import asdict, dataclass
from typing import Optional
from urllib.parse import quote, unquote, urlparse

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

    async def _request(
        self, method: str, path: str, *, params: dict, json: Optional[dict] = None,
        account_id: Optional[str] = None,
    ):
        headers = {"x-api-key": self.api_key, "Accept": "application/json"}
        if account_id:
            headers["Harness-Account"] = account_id
        last_error = None
        attempts = 2 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
                    response = await client.request(
                        method, f"{self.base_url}{path}", params=params, json=json, headers=headers
                    )
                if response.status_code in (429, 502, 503, 504) and attempt + 1 < attempts:
                    continue
                if response.status_code >= 400:
                    raise HarnessError(f"Harness API returned HTTP {response.status_code}.")
                if not response.content:
                    return {"http_status": response.status_code}
                try:
                    return response.json()
                except ValueError as exc:
                    raise HarnessError("Harness API returned an invalid JSON response.") from exc
            except httpx.TimeoutException as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    break
            except httpx.RequestError as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    break
        suffix = " after retrying" if attempts > 1 else ""
        raise HarnessError(f"Harness API request failed{suffix}.") from last_error

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
            account_id=ref.account_id,
        )

    async def get_waiting_approval(self, ref: ExecutionRef, node_execution_id: str):
        path = (
            f"/gateway/pipeline/api/v1/orgs/{quote(ref.org_id, safe='')}"
            f"/projects/{quote(ref.project_id, safe='')}/approvals/execution/"
            f"{quote(ref.execution_id, safe='')}"
        )
        payload = await self._request(
            "GET",
            path,
            params={
                "approval_status": "WAITING",
                "approval_type": "HarnessApproval",
                "node_execution_id": node_execution_id,
            },
            account_id=ref.account_id,
        )
        instances = parse_approval_instances(payload)
        matching = [
            instance for instance in instances
            if instance.get("node_execution_id") in (None, node_execution_id)
        ]
        if not matching:
            return None
        if len(matching) != 1:
            raise HarnessError("Harness returned multiple waiting approval instances for this step.")
        return matching[0]

    async def submit_decision(self, ref: ExecutionRef, approval_instance_id: str, action: str, comment: str):
        if action not in ("approve", "reject"):
            raise ValueError("Action must be approve or reject.")
        api_action = "APPROVE" if action == "approve" else "REJECT"
        return await self._request(
            "POST",
            f"/gateway/pipeline/api/approvals/{quote(approval_instance_id, safe='')}/harness/activity",
            params={"accountIdentifier": ref.account_id},
            json={"action": api_action, "comments": comment, "approverInputs": []},
            account_id=ref.account_id,
        )


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def parse_approval_instances(payload: dict) -> list[dict]:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        data = payload.get("data", payload.get("content"))
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("content", data.get("data"))
            if not isinstance(items, list):
                items = [data] if data.get("id") else []
        else:
            items = [payload] if payload.get("id") else []
    else:
        items = []

    instances = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            continue
        details = item.get("details") if isinstance(item.get("details"), dict) else {}
        node_id = (
            item.get("nodeExecutionId")
            or item.get("node_execution_id")
            or details.get("nodeExecutionId")
            or details.get("node_execution_id")
        )
        instances.append({
            "id": item["id"],
            "node_execution_id": node_id if isinstance(node_id, str) else None,
            "authorized_approvers": item.get("approvers", item.get("approverUsers", [])),
        })
    return instances


def summarize_execution(payload: dict, ref: ExecutionRef) -> dict:
    nodes = list(_walk(payload))
    approval_nodes = []
    for node in nodes:
        node_type = str(node.get("nodeType", "")).upper()
        item_type = str(node.get("type", "")).upper()
        step_type = str(node.get("stepType", "")).upper()
        if not (
            "APPROVAL" in node_type or "APPROVAL" in item_type or "APPROVAL" in step_type
        ):
            continue
        node_execution_id = next(
            (
                node.get(key) for key in
                ("nodeExecutionId", "node_execution_id", "uuid", "nodeExecutionUuid")
                if isinstance(node.get(key), str) and node.get(key)
            ),
            None,
        )
        approval_nodes.append({
            "node_execution_id": node_execution_id,
            "name": str(node.get("name", node.get("identifier", "Approval"))),
            "message": str(node.get("approvalMessage", node.get("message", ""))),
            "authorized_approvers": node.get("approvers", node.get("approverUsers", [])),
        })
    approval = approval_nodes[0] if approval_nodes else None
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
        "approval_pending": False,
        "approval_step_found": bool(approval_nodes),
        "approval_node_execution_id": approval["node_execution_id"] if approval else None,
        "approval_steps": approval_nodes,
        "approval_name": approval["name"] if approval else None,
        "approval_message": approval["message"] if approval else "",
        "authorized_approvers": approval["authorized_approvers"] if approval else [],
        "artifact": next(
            (str(node.get(k)) for node in nodes for k in ("artifactVersion", "tag", "imagePath") if node.get(k)),
            "Unknown",
        ),
        "service": next(
            (str(node.get(k)) for node in nodes for k in ("serviceName", "serviceIdentifier") if node.get(k)),
            "Unknown",
        ),
    }
