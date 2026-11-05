import asyncio

import pytest

from app.harness import (
    ExecutionRef,
    HarnessClient,
    parse_approval_instances,
    parse_execution_url,
    summarize_execution,
)


def test_parse_cd_execution_url():
    ref = parse_execution_url(
        "https://app.harness.io/ng/account/acct/cd/orgs/my-org/projects/my-project/"
        "pipelines/release/executions/exec-123/pipeline"
    )
    assert ref == ExecutionRef("acct", "my-org", "my-project", "release", "exec-123")


def test_parse_ci_execution_url():
    ref = parse_execution_url(
        "https://app.harness.io/ng/account/acct/ci/orgs/org/projects/project/"
        "pipelines/build/executions/exec-123"
    )
    assert ref.pipeline_id == "build"


@pytest.mark.parametrize("value", [
    "http://app.harness.io/ng/account/a/cd/orgs/o/projects/p/pipelines/x/executions/e",
    "https://evil.example/ng/account/a/cd/orgs/o/projects/p/pipelines/x/executions/e",
    "https://app.harness.io.evil.example/ng/account/a/cd/orgs/o/projects/p/pipelines/x/executions/e",
    "https://user@app.harness.io/ng/account/a/cd/orgs/o/projects/p/pipelines/x/executions/e",
    "https://app.harness.io/ng/account/a/cd/orgs/o/projects/p/pipelines/x/executions/e?redirect=https://evil.example",
    "https://app.harness.io/ng/account/a/cd/orgs/o/projects/p/pipelines/x",
])
def test_reject_invalid_execution_url(value):
    with pytest.raises(ValueError):
        parse_execution_url(value)


def test_summarize_pending_uat_approval():
    ref = ExecutionRef("acct", "org", "project", "pipeline", "execution")
    summary = summarize_execution({
        "data": {
            "pipelineExecutionSummary": {
                "pipelineName": "Release",
                "status": "RUNNING",
                "triggeredBy": "jane@example.com",
                "environmentName": "UAT",
                "approvalInstances": [{
                    "type": "APPROVAL",
                    "status": "WAITING",
                    "name": "UAT approval",
                    "uuid": "node-1",
                    "approvalMessage": "Verify the build",
                    "approvers": ["qa@example.com"],
                }],
            }
        }
    }, ref)
    assert summary["approval_pending"] is False
    assert summary["approval_step_found"] is True
    assert summary["approval_node_execution_id"] == "node-1"
    assert summary["environment"] == "UAT"
    assert summary["pipeline_name"] == "Release"
    assert summary["triggered_by"] == "jane@example.com"
    assert summary["authorized_approvers"] == ["qa@example.com"]


def test_summarize_completed_approval_is_not_pending():
    ref = ExecutionRef("acct", "org", "project", "pipeline", "execution")
    summary = summarize_execution({
        "approval": {"type": "APPROVAL", "status": "APPROVED"},
        "environment": "UAT",
    }, ref)
    assert summary["approval_pending"] is False


def test_parse_waiting_approval_instance():
    instances = parse_approval_instances({
        "data": {"content": [{
            "id": "approval-1",
            "node_execution_id": "node-1",
            "approvers": ["qa@example.com"],
        }]}
    })
    assert instances == [{
        "id": "approval-1",
        "node_execution_id": "node-1",
        "authorized_approvers": ["qa@example.com"],
    }]


def test_harness_approval_api_paths_and_payload(monkeypatch):
    calls = []

    class Response:
        def __init__(self, status_code, payload=None):
            self.status_code = status_code
            self.content = b"" if status_code == 204 else b"{}"
            self.payload = payload

        def json(self):
            return self.payload

    class AsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            if method == "GET":
                return Response(200, {"data": {"content": [{"id": "approval-1", "node_execution_id": "node-1"}]}})
            return Response(204)

    monkeypatch.setattr("app.harness.httpx.AsyncClient", AsyncClient)
    client = HarnessClient(api_key="test-key")
    ref = ExecutionRef("acct", "org", "project", "pipeline", "execution")

    async def perform_calls():
        instance = await client.get_waiting_approval(ref, "node-1")
        response = await client.submit_decision(ref, instance["id"], "approve", "LGTM")
        return instance, response

    instance, response = asyncio.run(perform_calls())
    assert instance["id"] == "approval-1"
    assert response == {"http_status": 204}
    assert calls[0][0] == "GET"
    assert calls[0][1] == (
        "https://app.harness.io/gateway/pipeline/api/v1/orgs/org/projects/project/approvals/execution/execution"
    )
    assert calls[0][2]["params"]["approval_status"] == "WAITING"
    assert calls[0][2]["params"]["approval_type"] == "HarnessApproval"
    assert calls[0][2]["params"]["node_execution_id"] == "node-1"
    assert calls[0][2]["headers"]["Harness-Account"] == "acct"
    assert calls[1][0] == "POST"
    assert calls[1][1] == "https://app.harness.io/gateway/pipeline/api/approvals/approval-1/harness/activity"
    assert calls[1][2]["params"]["accountIdentifier"] == "acct"
    assert calls[1][2]["json"] == {
        "action": "APPROVE",
        "comments": "LGTM",
        "approverInputs": [],
    }
