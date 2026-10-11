"""Authenticated intake and report reads. Investigation runs in another process."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError

from .config import Principal, Settings
from .contracts import Investigation, Window
from .store import Conflict, QueueFull, Store
from .tools import redact

BODY_LIMIT = 256_000


class BodyLimit:
    """Bound chunked as well as Content-Length bodies before JSON parsing."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        chunks = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > BODY_LIMIT:
                return await JSONResponse({"detail": "body too large"}, status_code=413)(scope, receive, send)
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": b"".join(chunks), "more_body": False}

        await self.app(scope, replay, send)


def create_app(settings: Settings) -> FastAPI:
    settings.check_secrets()
    store = Store(settings.database)
    app = FastAPI(title="Incident Triage Automation", version="1.0", docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.add_middleware(BodyLimit)
    app.state.store = store
    bearer = HTTPBearer(auto_error=False)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Do not echo user-supplied credentials or payloads in framework errors.
        return JSONResponse({"detail": "request does not match the contract"}, status_code=422)

    def principal(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Principal:
        if credentials and credentials.scheme.lower() == "bearer":
            for candidate in settings.principals:
                expected = os.environ.get(candidate.token_env, "")
                if expected and hmac.compare_digest(credentials.credentials.encode(), expected.encode()):
                    return candidate
        raise HTTPException(401, "invalid credentials", headers={"WWW-Authenticate": "Bearer"})

    def authorize(actor, request):
        if request.service not in actor.services or request.environment not in actor.environments:
            raise HTTPException(403, "scope not permitted")
        try:
            binding = settings.binding(actor.tenant, request.service, request.environment)
        except PermissionError:
            raise HTTPException(403, "scope not configured") from None
        if settings.mode == "anthropic" and not binding.allow_hosted_model:
            raise HTTPException(403, "hosted inference is not approved for this scope")

    def lookup(run_id, actor):
        row = store.get(run_id, actor.tenant)
        if row:
            request = Investigation.model_validate_json(row["request"])
            if request.service in actor.services and request.environment in actor.environments:
                return row
        raise HTTPException(404, "run not found")

    def accept(actor, key, request, payload_hash=None):
        authorize(actor, request)
        payload_hash = payload_hash or hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        # Redaction precedes durable storage and hosted inference.
        request = Investigation.model_validate(redact(request.model_dump(mode="json")))
        try:
            row, created = store.submit(actor.tenant, actor.token_env, key, request, settings.revision,
                                        settings.max_pending_per_tenant, payload_hash=payload_hash)
        except Conflict:
            raise HTTPException(409, "idempotency key has a different payload") from None
        except QueueFull:
            raise HTTPException(429, "tenant queue capacity reached", headers={"Retry-After": "10"}) from None
        # A reused key must not reveal a run outside this principal's scope.
        lookup(row["id"], actor)
        return JSONResponse({"run_id": row["id"], "status": row["status"], "created": created},
                            status_code=202, headers={"Location": f"/v1/investigations/{row['id']}"})

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready():
        try:
            with store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("SELECT id FROM runs LIMIT 1")
                db.rollback()
            return {"status": "ok", "accepts": "durable intake"}
        except Exception:
            raise HTTPException(503, "state store unavailable") from None

    @app.get("/v1/openapi.json")
    def schema(actor: Principal = Depends(principal)):
        return app.openapi()

    @app.post("/v1/investigations")
    def submit(request: Investigation, actor: Principal = Depends(principal),
               idempotency_key: str = Header(min_length=1, max_length=200)):
        return accept(actor, "api:" + idempotency_key, request)

    @app.get("/v1/investigations/{run_id}")
    def detail(run_id: str, actor: Principal = Depends(principal)):
        row = lookup(run_id, actor)
        return {"run_id": row["id"], "status": row["status"], "request": json.loads(row["request"]),
                "config_revision": row["config_revision"], "attempts": row["attempts"], "steps": row["steps"],
                "usage": {k: row[k] for k in ("model_calls", "input_tokens", "output_tokens")},
                "report": json.loads(row["report"]) if row["report"] else None, "error": row["error"],
                "evidence": [e.model_dump(mode="json") for e in store.evidence(run_id)]}

    @app.get("/v1/investigations/{run_id}/events")
    def events(run_id: str, after: int = Query(default=0, ge=0), actor: Principal = Depends(principal)):
        lookup(run_id, actor)
        items = store.events(run_id, after)
        return {"events": items, "next_cursor": items[-1]["seq"] if items else after}

    @app.post("/v1/investigations/{run_id}/cancel")
    def cancel(run_id: str, actor: Principal = Depends(principal)):
        lookup(run_id, actor)
        store.cancel(run_id, actor.tenant)
        return {"run_id": run_id, "status": lookup(run_id, actor)["status"]}

    @app.post("/v1/integrations/pagerduty/{subscription}/events")
    async def pagerduty(subscription: str, request: Request):
        actor = next((p for p in settings.principals if p.token_env == subscription and p.pagerduty_secret_env), None)
        if actor is None:
            raise HTTPException(401, "unknown subscription")
        body = await request.body()
        secret = os.environ.get(actor.pagerduty_secret_env, "")
        expected = "v1=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        signatures = request.headers.get("X-PagerDuty-Signature", "").split(",")
        if not secret or not any(hmac.compare_digest(expected.encode(), s.strip().encode()) for s in signatures):
            raise HTTPException(401, "invalid webhook signature")
        try:
            event = json.loads(body)["event"]
            if event["event_type"] != "incident.triggered":
                return {"status": "ignored", "reason": "only incident.triggered starts triage"}
            data = event["data"]
            source_id = data["service"]["id"]
            matches = [b for b in settings.bindings if b.tenant == actor.tenant
                       and b.pagerduty_service_id == source_id and b.service in actor.services
                       and b.environment in actor.environments]
            if len(matches) != 1:
                raise HTTPException(403, "PagerDuty service must map to exactly one authorized binding")
            event_id = event["id"]
            if not isinstance(event_id, str) or not 1 <= len(event_id) <= 200:
                raise ValueError("invalid event ID")
            occurred = datetime.fromisoformat(event.get("occurred_at", data["created_at"]).replace("Z", "+00:00"))
            normalized = Investigation(service=matches[0].service, environment=matches[0].environment,
                                       question=data["title"], window=Window(start=occurred - timedelta(minutes=15),
                                                                           end=occurred))
        except (ValueError, KeyError, TypeError, AttributeError, ValidationError):
            raise HTTPException(422, "invalid PagerDuty event") from None
        # Compare the entire signed event, including fields omitted by normalization.
        digest = hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
        return accept(actor, f"pagerduty:{subscription}:{event_id}", normalized, digest)

    return app
