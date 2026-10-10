"""Independent worker process; HTTP request completion never owns execution."""
from __future__ import annotations

import threading

import httpx

from .agent import AnthropicPlanner, fallback, investigate
from .contracts import Investigation, Report
from .store import LostLease, Store
from .tools import ToolGateway


def authorized(settings, run, request):
    return any(p.token_env == run["actor"] and p.tenant == run["tenant"] and request.service in p.services
               and request.environment in p.environments for p in settings.principals)


def process_one(settings, store: Store, client=None, planner=None) -> bool:
    run = store.claim(settings.run_seconds, settings.max_attempts)
    if run is None:
        return False
    if run.get("terminal"):
        return True
    stop = threading.Event()

    def renew():
        while not stop.wait(3):
            try:
                store.heartbeat(run)
            except Exception:
                return  # Fencing prevents stale workers committing after lost ownership.

    heartbeat = threading.Thread(target=renew, daemon=True)
    heartbeat.start()
    owned_client = client is None
    client = client or httpx.Client(trust_env=False)
    try:
        request = Investigation.model_validate_json(run["request"])
        if settings.revision != run["config_revision"] or not authorized(settings, run, request):
            store.finish(run, "failed", error="configuration or authorization changed; submit a new run")
            return True
        binding = settings.binding(run["tenant"], request.service, request.environment)
        if settings.mode == "anthropic":
            if not binding.allow_hosted_model:
                raise PermissionError("hosted inference not approved for this binding")
            planner = planner or AnthropicPlanner(settings, client)
        investigate(run, request, settings, store, ToolGateway(binding, client), planner)
    except LostLease:
        pass  # Cancellation or another worker owns the run; do not overwrite it.
    except TimeoutError:
        try:
            store.finish(run, "timed_out", error="investigation deadline exhausted")
        except LostLease:
            pass
    except Exception:
        # No raw provider errors, URLs with credentials, or payloads in public state/logs.
        try:
            evidence = store.evidence(run["id"])
            if evidence:
                findings = fallback(evidence, ["Investigation interrupted by a provider or worker failure."])
                report = Report(**findings.model_dump(), run_id=run["id"], service=request.service,
                                environment=request.environment, window=request.window,
                                mode=settings.mode, model_id=settings.model if settings.mode == "anthropic" else None,
                                status="partial")
                store.finish(run, "partial", report, error="investigation interrupted")
            else:
                store.finish(run, "failed", error="investigation failed before usable evidence was collected")
        except TimeoutError:
            try:
                store.finish(run, "timed_out", error="investigation deadline exhausted")
            except LostLease:
                pass
        except LostLease:
            pass
    finally:
        stop.set()
        heartbeat.join(timeout=1)
        if owned_client:
            client.close()
    return True
