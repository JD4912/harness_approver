import pytest

from app.harness import ExecutionRef, parse_execution_url, summarize_execution


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
                    "approvalMessage": "Verify the build",
                    "approvers": ["qa@example.com"],
                }],
            }
        }
    }, ref)
    assert summary["approval_pending"] is True
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
