import csv
import io
import json
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import timezone
from pathlib import Path

from authlib.integrations.starlette_client import OAuth
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.auth import User, current_user, require_csrf, require_role, roles_from_claims
from app.config import settings
from app.database import Base, SessionLocal, engine
from app.harness import HarnessClient, HarnessError, parse_execution_url, summarize_execution
from app.models import AuditEvent


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(bind=engine)
    if settings.database_url.startswith("sqlite"):
        with engine.begin() as connection:
            connection.execute(text(
                "CREATE TRIGGER IF NOT EXISTS audit_events_no_update "
                "BEFORE UPDATE ON audit_events BEGIN SELECT RAISE(ABORT, 'audit events are immutable'); END"
            ))
            connection.execute(text(
                "CREATE TRIGGER IF NOT EXISTS audit_events_no_delete "
                "BEFORE DELETE ON audit_events BEGIN SELECT RAISE(ABORT, 'audit events are immutable'); END"
            ))
    elif engine.dialect.name == "postgresql":
        with engine.begin() as connection:
            connection.execute(text(
                "CREATE OR REPLACE FUNCTION reject_audit_mutation() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'audit events are immutable'; END; $$"
            ))
            connection.execute(text(
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_trigger "
                "WHERE tgname = 'audit_events_no_mutation') THEN "
                "CREATE TRIGGER audit_events_no_mutation BEFORE UPDATE OR DELETE ON audit_events "
                "FOR EACH ROW EXECUTE FUNCTION reject_audit_mutation(); END IF; END $$"
            ))
    yield


app = FastAPI(title="Harness Deployment Approval Manager", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.app_secret_key or "development-only-not-for-deployment",
    https_only=settings.session_https_only,
    same_site="lax",
)
APP_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
_rate_buckets: dict[str, deque[float]] = defaultdict(deque)


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    if request.url.path not in {"/login", "/dev-login", "/api/lookup", "/api/decision"}:
        return await call_next(request)
    key = request.client.host if request.client else "unknown"
    now = time.monotonic()
    bucket = _rate_buckets[key]
    while bucket and now - bucket[0] >= 60:
        bucket.popleft()
    if len(bucket) >= 60:
        return JSONResponse(
            {"detail": "Rate limit exceeded. Try again in a minute."},
            status_code=429,
            headers={"Retry-After": "60"},
        )
    bucket.append(now)
    if len(_rate_buckets) > 2048:
        for stale_key in [name for name, entries in _rate_buckets.items() if not entries or now - entries[-1] >= 60]:
            _rate_buckets.pop(stale_key, None)
    return await call_next(request)


templates = Jinja2Templates(directory=APP_DIR / "templates")
oauth = OAuth()
if settings.app_env != "development":
    oauth.register(
        name="oidc",
        server_metadata_url=f"{settings.oidc_issuer}/.well-known/openid-configuration",
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        client_kwargs={"scope": "openid profile email"},
    )


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _csrf(request: Request):
    if "csrf_token" not in request.session:
        request.session["csrf_token"] = secrets.token_urlsafe(32)
    return request.session["csrf_token"]


def _audit(db: Session, user: User, ref, action: str, environment: str, comment: str, response: dict):
    db.add(AuditEvent(
        actor_email=user.email,
        actor_name=user.name,
        action=action,
        environment=environment,
        account_id=ref.account_id,
        org_id=ref.org_id,
        project_id=ref.project_id,
        pipeline_id=ref.pipeline_id,
        execution_id=ref.execution_id,
        comment=comment,
        api_response=json.dumps(response, separators=(",", ":"), ensure_ascii=True),
    ))
    db.commit()


async def _load_execution(client: HarnessClient, ref):
    summary = summarize_execution(await client.get_execution(ref), ref)
    waiting = []
    for step in summary["approval_steps"]:
        node_execution_id = step["node_execution_id"]
        if not node_execution_id:
            continue
        instance = await client.get_waiting_approval(ref, node_execution_id)
        if instance:
            waiting.append((step, instance))
    if len(waiting) > 1:
        raise HarnessError("Multiple approval steps are waiting; this execution is ambiguous.")
    if waiting:
        step, instance = waiting[0]
        summary["approval_pending"] = True
        summary["approval_node_execution_id"] = step["node_execution_id"]
        summary["approval_instance_id"] = instance["id"]
        summary["approval_name"] = step["name"]
        summary["approval_message"] = step["message"]
        if instance["authorized_approvers"]:
            summary["authorized_approvers"] = instance["authorized_approvers"]
    return summary


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, db: Session = Depends(get_db)):
    identity = request.session.get("user")
    if not identity:
        return RedirectResponse("/login", status_code=303)
    user = User(
        email=identity["email"],
        name=identity.get("name") or identity["email"],
        roles=frozenset(identity.get("roles", [])),
    )
    events = db.query(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(50).all()
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"user": user, "csrf": _csrf(request), "events": events},
    )


@app.get("/login")
async def login(request: Request):
    if settings.app_env == "development":
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"dev_login": True, "csrf": _csrf(request)},
            status_code=200,
        )
    return await oauth.oidc.authorize_redirect(request, settings.oidc_redirect_uri)


@app.get("/login/csrf")
async def login_csrf(request: Request):
    if settings.app_env != "development":
        raise HTTPException(status_code=404)
    return {"csrf_token": _csrf(request)}


@app.post("/dev-login")
async def dev_login(request: Request, csrf_token: str = Form(...)):
    if settings.app_env != "development":
        raise HTTPException(status_code=404)
    require_csrf(request, csrf_token)
    request.session.clear()
    request.session["user"] = {
        "email": settings.dev_user_email,
        "name": settings.dev_user_name,
        "roles": list(settings.dev_user_roles),
    }
    _csrf(request)
    return RedirectResponse("/", status_code=303)


@app.get("/auth/callback")
async def auth_callback(request: Request):
    if settings.app_env == "development":
        raise HTTPException(status_code=404)
    token = await oauth.oidc.authorize_access_token(request)
    claims = token.get("userinfo") or await oauth.oidc.parse_id_token(request, token)
    email = claims.get("email")
    if not email or claims.get("email_verified") is not True:
        raise HTTPException(status_code=403, detail="A verified email claim is required.")
    request.session.clear()
    request.session["user"] = {
        "email": email,
        "name": claims.get("name") or email,
        "roles": list(roles_from_claims(claims)),
    }
    _csrf(request)
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
async def logout(request: Request, csrf_token: str = Form(...), _user: User = Depends(current_user)):
    require_csrf(request, csrf_token)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.post("/api/lookup")
async def lookup(
    request: Request,
    execution_url: str = Form(...),
    csrf_token: str = Form(...),
    user: User = Depends(current_user),
):
    require_role(user, "requester", "uat_approver", "prod_approver", "admin", "auditor")
    require_csrf(request, csrf_token)
    try:
        ref = parse_execution_url(execution_url)
        summary = await _load_execution(HarnessClient(), ref)
        if not summary["approval_pending"]:
            if not summary["approval_step_found"]:
                detail = "No approval step was found in the execution."
            elif not summary["approval_node_execution_id"]:
                detail = "Could not identify the approval step execution ID."
            else:
                detail = "No pending approval instance was found for this execution."
            raise HTTPException(
                status_code=409,
                detail=detail,
            )
        if summary["environment"] != "UAT":
            raise HTTPException(
                status_code=409,
                detail="Phase 1 supports UAT approvals only; Production approval is disabled.",
            )
        return JSONResponse({"execution": summary, "csrf_token": request.session["csrf_token"]})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HarnessError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/decision")
async def decision(
    request: Request,
    execution_url: str = Form(...),
    action: str = Form(...),
    comment: str = Form(...),
    csrf_token: str = Form(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    require_role(user, "uat_approver", "admin")
    require_csrf(request, csrf_token)
    comment = comment.strip()
    if action not in {"approve", "reject"}:
        raise HTTPException(status_code=400, detail="Decision must be approve or reject.")
    if not comment or len(comment) > 2000:
        raise HTTPException(status_code=400, detail="A comment of 1–2000 characters is required.")
    try:
        ref = parse_execution_url(execution_url)
        client = HarnessClient()
        execution = await _load_execution(client, ref)
        if execution["environment"] != "UAT":
            raise HTTPException(status_code=403, detail="Production approval is disabled in Phase 1.")
        if not execution["approval_pending"]:
            raise HTTPException(status_code=409, detail="This execution has no pending approval.")
        harness_comment = f"{comment}\n\n[Approval Manager user: {user.email}]"
        if len(harness_comment) > 2000:
            raise HTTPException(status_code=400, detail="Comment is too long after adding approver identity.")
        try:
            api_response = await client.submit_decision(
                ref, execution["approval_instance_id"], action, harness_comment
            )
        except HarnessError as exc:
            _audit(
                db, user, ref, f"{action}_failed", execution["environment"], comment,
                {"error": str(exc)},
            )
            raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HarnessError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    _audit(db, user, ref, action, execution["environment"], comment, api_response)
    return {"ok": True, "message": f"Execution {action}d.", "api_response": api_response}


@app.get("/api/audit.csv")
async def export_audit(user: User = Depends(current_user), db: Session = Depends(get_db)):
    require_role(user, "auditor", "admin")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "id", "timestamp_utc", "actor_email", "actor_name", "environment", "action",
        "account_id", "org_id", "project_id", "pipeline_id", "execution_id", "comment", "api_response",
    ])
    def safe_cell(value):
        rendered = "" if value is None else str(value)
        if rendered.startswith(("=", "+", "-", "@", "\t", "\r")):
            return "'" + rendered
        return rendered

    for event in db.query(AuditEvent).order_by(AuditEvent.created_at.desc()).all():
        created_at = event.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        writer.writerow([
            event.id, created_at.isoformat(), *(safe_cell(value) for value in (
                event.actor_email, event.actor_name, event.environment, event.action, event.account_id,
                event.org_id, event.project_id, event.pipeline_id, event.execution_id, event.comment,
                event.api_response,
            )),
        ])
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=approval-audit.csv"},
    )


@app.get("/health")
async def health():
    return {"status": "ok"}
