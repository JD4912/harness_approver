const lookupForm = document.querySelector("#lookup-form");
const decisionForm = document.querySelector("#decision-form");
const card = document.querySelector("#execution-card");
const notice = document.querySelector("#notice");
let csrfToken = document.querySelector('input[name="csrf_token"]')?.value || "";

function showNotice(message, error = false) {
  notice.textContent = message;
  notice.className = error ? "notice error" : "notice success";
}

async function postForm(url, data) {
  const formData = new FormData();
  for (const [key, value] of Object.entries(data)) formData.append(key, value);
  formData.append("csrf_token", csrfToken);
  const response = await fetch(url, { method: "POST", body: formData, credentials: "same-origin" });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || "Request failed.");
  if (payload.csrf_token) csrfToken = payload.csrf_token;
  return payload;
}

lookupForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  card.hidden = true;
  showNotice("Checking Harness execution…");
  try {
    const result = await postForm("/api/lookup", {
      execution_url: lookupForm.elements.execution_url.value,
    });
    const execution = result.execution;
    document.querySelector("#pipeline-name").textContent = execution.pipeline_name;
    document.querySelector("#pipeline-id").textContent = execution.pipeline_id;
    document.querySelector("#execution-id").textContent = execution.execution_id;
    document.querySelector("#triggered-by").textContent = execution.triggered_by;
    document.querySelector("#execution-status").textContent = execution.status;
    document.querySelector("#service").textContent = execution.service;
    document.querySelector("#artifact").textContent = execution.artifact;
    document.querySelector("#approval-name").textContent = execution.approval_name;
    document.querySelector("#approvers").textContent = Array.isArray(execution.authorized_approvers)
      ? execution.authorized_approvers.join(", ") || "Not provided by Harness"
      : execution.authorized_approvers || "Not provided by Harness";
    document.querySelector("#approval-message").textContent = execution.approval_message;
    card.hidden = false;
    showNotice("Execution loaded. Review the details before deciding.");
  } catch (error) {
    showNotice(error.message, true);
  }
});

decisionForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const action = event.submitter?.value;
  const comment = decisionForm.elements.comment.value.trim();
  if (!comment) return showNotice("A decision comment is required.", true);
  if (!window.confirm(`Confirm ${action} for this UAT execution?`)) return;
  try {
    const result = await postForm("/api/decision", {
      execution_url: lookupForm.elements.execution_url.value,
      action,
      comment,
    });
    showNotice(result.message);
    card.hidden = true;
    window.setTimeout(() => window.location.reload(), 700);
  } catch (error) {
    showNotice(error.message, true);
  }
});
