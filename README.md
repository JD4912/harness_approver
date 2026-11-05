# Harness Deployment Approval Manager

Phase 1 MVP for reviewing Harness pipeline executions and taking a human-confirmed UAT approval decision. Production actions are deliberately disabled. This application uses its own OIDC-authenticated user identity and records that identity in the Harness comment and audit event.

## Run locally

1. Use Python 3.9+ and install dependencies:

   ```sh
   python -m venv .venv
   . .venv/bin/activate
   pip install -r requirements.txt
   ```
2. Copy `.env.example` to `.env` and set `HARNESS_API_KEY` to a dedicated, minimum-permission Harness API key. Keep the key out of source control.
3. Start the server:

   ```sh
   uvicorn app.main:app --reload
   ```
4. Open <http://127.0.0.1:8000>. In development, the local login uses the configured `DEV_USER_*` values. Development login is not available outside `APP_ENV=development`.

The local database defaults to `approvals.db`. Set `DATABASE_URL` to a PostgreSQL SQLAlchemy URL for a shared deployment. `HARNESS_BASE_URL` is an HTTPS origin and defaults to `https://app.harness.io`; for self-managed Harness, set it to the approved HTTPS origin.

## Production setup

- Set `APP_ENV=production`, `APP_SECRET_KEY` (at least 32 characters), `SESSION_HTTPS_ONLY=true`, and configure `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, and `OIDC_REDIRECT_URI`.
- Configure `OIDC_ROLES_CLAIM` and `OIDC_ROLE_MAP` as comma-separated `identity-provider-group=application-role` pairs. Supported roles: `requester`, `uat_approver`, `prod_approver`, `admin`, and `auditor`. The MVP exposes decision actions only to `uat_approver` and `admin`; `prod_approver` does not enable Production actions.
- Inject the Harness API key from a secrets manager at runtime. Use a dedicated Harness service account limited to execution read and approval actions.
- Terminate TLS at a trusted ingress, keep the app and database private, and use a PostgreSQL database credential that cannot update or delete audit rows. The app also installs database-level mutation-blocking triggers for the audit table.
- The Docker image runs Uvicorn on port 8000. Configure health checks against `/health`; persist the database outside the container.

## Harness integration

The app validates the URL origin against `HARNESS_BASE_URL` and accepts Harness CD/CI execution paths only. API calls are made to that configured origin, never to the submitted URL. It uses:

- `GET /pipeline/api/pipelines/execution/v2/{planExecutionId}` with account, org, and project identifiers.
- `POST /pipeline/api/pipeline/execution/{planExecutionId}/approval` with `status` (`APPROVED` or `REJECTED`) and `comments`.

Harness API payloads can vary by module and version. The execution summarizer recognizes common execution and approval fields; if it cannot identify a pending approval or UAT environment, the app fails closed. Verify these endpoints and the response shape against the target Harness tenant/version before enabling real approvals. API calls have an 8-second timeout and retry transient failures once.

## Scope and safeguards

- URL lookup, execution summary, human-confirmed approve/reject, mandatory comments, UAT role checks, audit history, and auditor/admin CSV export are implemented.
- Production approval, change tickets/windows, multi-approver policy, notifications, webhook intake, and richer dashboard filters are not enabled in this phase.
- CSRF tokens, same-site session cookies, HTTPS-only production cookies, OIDC role mapping, origin allowlisting, and API error reporting are enabled.
- Audit rows cannot be updated/deleted through SQLite or PostgreSQL triggers. Back up the database and restrict database administrator access; no application-level mechanism can protect records from a privileged database owner.

## Tests

```sh
pytest -q
```
# harness_approver
