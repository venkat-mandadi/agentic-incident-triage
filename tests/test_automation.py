"""Acceptance tests for durable, scoped incident-triage automation."""
import hashlib
import hmac
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from incident_triage.automation.agent import AnthropicPlanner
from incident_triage.automation.api import create_app
from incident_triage.automation.config import Settings
from incident_triage.automation.contracts import Decision, Investigation
from incident_triage.automation.store import LostLease, Store
from incident_triage.automation.tools import ToolGateway
from incident_triage.automation.worker import process_one

EXAMPLES = Path(__file__).parent.parent / "examples"
TOKEN = "test-token-with-at-least-32-characters"


@pytest.fixture
def request_data():
    return json.loads((EXAMPLES / "investigation.json").read_text())


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_TOKEN", TOKEN)
    return Settings(database=str(tmp_path / "jobs.db"),
                    principals=[{"token_env": "TEST_TOKEN", "tenant": "one", "services": ["payments-api"],
                                 "environments": ["demo"]}],
                    bindings=[{"tenant": "one", "service": "payments-api", "environment": "demo",
                               "snapshot_path": str(EXAMPLES / "sample_signals.json")}])


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as client:
        client.headers["Authorization"] = "Bearer " + TOKEN
        yield client


def submit(client, request_data, key="test"):
    return client.post("/v1/investigations", json=request_data, headers={"Idempotency-Key": key})


def queued(settings, request_data):
    store = Store(settings.database)
    request = Investigation.model_validate(request_data)
    row, _ = store.submit("one", "TEST_TOKEN", "test", request, settings.revision)
    return store, row


def test_end_to_end_intake_worker_report_and_progress(client, settings, request_data):
    response = submit(client, request_data)
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    path = f"/v1/investigations/{run_id}"
    assert client.get(path).json()["status"] == "queued"
    assert process_one(settings, client.app.state.store)
    result = client.get(path).json()
    assert result["status"] == "partial"  # Fixture data is explicitly not live.
    assert result["report"]["incident_resolved"] is None
    assert result["report"]["human_review_required"] is True
    assert result["report"]["hypotheses"][0]["confidence"] == "low"
    assert result["report"]["observations"][0]["evidence_ids"] == [result["evidence"][0]["id"]]
    progress = client.get(path + "/events").json()
    assert progress["events"][-1]["kind"] == "partial"
    assert client.get(path + "/events", params={"after": progress["next_cursor"]}).json()["events"] == []
    assert not process_one(settings, client.app.state.store)


def test_concurrent_idempotency_creates_one_logical_run(client, request_data):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: submit(client, request_data), range(16)))
    assert all(r.status_code == 202 for r in results)
    assert len({r.json()["run_id"] for r in results}) == 1
    assert sum(r.json()["created"] for r in results) == 1
    changed = {**request_data, "question": "Different question"}
    assert submit(client, changed).status_code == 409


def test_authentication_and_scope(client, request_data):
    assert client.post("/v1/investigations", json=request_data,
                       headers={"Authorization": "Bearer invalid", "Idempotency-Key": "x"}).status_code == 401
    assert submit(client, {**request_data, "tenant_id": "other"}).status_code == 422
    assert submit(client, {**request_data, "service": "secret-service"}).status_code == 403
    assert submit(client, {**request_data, "environment": "production"}).status_code == 403
    assert client.get("/v1/openapi.json").status_code == 200


def test_cross_tenant_and_same_tenant_service_reads_denied(client, settings, request_data, monkeypatch):
    from incident_triage.automation.config import Principal
    run_id = submit(client, request_data).json()["run_id"]
    monkeypatch.setenv("OTHER", "other-token-with-at-least-32-characters")
    for tenant, services in [("two", ["payments-api"]), ("one", ["unrelated"])]:
        settings.principals.append(Principal(token_env="OTHER", tenant=tenant, services=services,
                                              environments=["demo"]))
        headers = {"Authorization": "Bearer other-token-with-at-least-32-characters"}
        path = f"/v1/investigations/{run_id}"
        assert client.get(path, headers=headers).status_code == 404
        assert client.get(path + "/events", headers=headers).status_code == 404
        assert client.post(path + "/cancel", headers=headers).status_code == 404
        settings.principals.pop()


def test_queue_capacity_retries_and_body_limit(client, settings, request_data):
    settings.max_pending_per_tenant = 1
    assert submit(client, request_data).status_code == 202
    assert submit(client, request_data).status_code == 202
    assert submit(client, request_data, "another").status_code == 429
    assert client.post("/v1/investigations", content=b"x" * 256001).status_code == 413


@pytest.mark.parametrize("window", [
    {"start": "2026-07-25T14:00:00", "end": "2026-07-25T14:10:00"},
    {"start": "2026-07-25T14:00:00Z", "end": "2026-07-25T16:10:00Z"},
    {"start": "2026-07-25T14:00:00Z", "end": "2026-07-25T13:10:00Z"},
])
def test_invalid_time_windows(client, request_data, window):
    assert submit(client, {**request_data, "window": window}).status_code == 422


def test_cancel_queued_run_is_idempotent(client, settings, request_data):
    run_id = submit(client, request_data).json()["run_id"]
    for _ in range(2):
        assert client.post(f"/v1/investigations/{run_id}/cancel").json()["status"] == "cancelled"
    assert not process_one(settings, client.app.state.store)


def test_running_cancellation_fences_stale_worker(settings, request_data):
    store, row = queued(settings, request_data)
    claimed = store.claim(120)
    store.cancel(row["id"], "one")
    with pytest.raises(LostLease):
        store.finish(claimed, "completed")


def test_crash_recovery_reuses_checkpoint_and_fences_old_worker(settings, request_data):
    store, row = queued(settings, request_data)
    old = store.claim(120)
    with httpx.Client() as client:
        evidence = ToolGateway(settings.bindings[0], client).execute(
            "signals.correlate", Investigation.model_validate(request_data), old["deadline"])
    store.save_evidence(old, evidence)
    with store.connect() as db:
        db.execute("UPDATE runs SET lease_until=0 WHERE id=?", (row["id"],))
    recovered = Store(settings.database)
    assert process_one(settings, recovered)
    assert recovered.get(row["id"], "one")["attempts"] == 2
    assert [e.id for e in recovered.evidence(row["id"])] == [evidence.id]
    with pytest.raises(LostLease):
        store.save_evidence(old, evidence)


def test_deadline_and_recovery_budget(settings, request_data):
    store, row = queued(settings, request_data)
    store.claim(120)
    with store.connect() as db:
        db.execute("UPDATE runs SET lease_until=0,deadline=0 WHERE id=?", (row["id"],))
    assert process_one(settings, store)
    assert store.get(row["id"], "one")["status"] == "timed_out"


def test_config_change_fails_closed(settings, request_data):
    store, row = queued(settings, request_data)
    settings.max_steps += 1
    process_one(settings, store)
    assert store.get(row["id"], "one")["status"] == "failed"
    assert not store.evidence(row["id"])


def test_step_budget_partial_result(settings, request_data):
    settings.max_steps = 1
    store, row = queued(settings, request_data)
    process_one(settings, store)
    result = store.get(row["id"], "one")
    assert result["steps"] == 1
    assert "budget exhausted" in result["report"]


def test_pagerduty_signature_mapping_and_event_dedup(settings, request_data, monkeypatch):
    settings.principals[0].pagerduty_secret_env = "PD_SECRET"
    settings.bindings[0].pagerduty_service_id = "PSERVICE"
    monkeypatch.setenv("PD_SECRET", "test-signing-secret")
    payload = json.loads((EXAMPLES / "sample_alert.json").read_text())
    payload["event"]["data"]["service"]["id"] = "PSERVICE"
    body = json.dumps(payload).encode()
    signature = "v1=" + hmac.new(b"test-signing-secret", body, hashlib.sha256).hexdigest()
    path = "/v1/integrations/pagerduty/TEST_TOKEN/events"
    with TestClient(create_app(settings)) as client:
        assert client.post(path, content=body).status_code == 401
        headers = {"X-PagerDuty-Signature": "v1=old, " + signature}
        one = client.post(path, content=body, headers=headers)
        two = client.post(path, content=body, headers=headers)
        assert one.status_code == two.status_code == 202
        assert one.json()["run_id"] == two.json()["run_id"]
        assert client.post(path, content=body + b" ", headers=headers).status_code == 401


def configured_query(settings, kind="prometheus"):
    from incident_triage.automation.config import Query
    settings.bindings[0].snapshot_path = None
    settings.bindings[0].queries = [Query(name="errors", kind=kind, base_url="https://telemetry.example.test",
                                        expression='{service="payments-api",environment="demo"}')]
    return settings.bindings[0]


@pytest.mark.parametrize("response,status", [
    (httpx.Response(200, json={"status": "success", "data": {"resultType": "matrix", "result": []}}), "empty"),
    (httpx.Response(403, json={"error": "secret=do-not-leak"}), "denied"),
    (httpx.Response(503, json={"error": "backend failed"}), "error"),
    (httpx.Response(200, json={"status": "error", "data": {}}), "error"),
    (httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": []}}), "error"),
    (httpx.Response(200, content=b"x" * 256001), "error"),
    (httpx.Response(302, headers={"Location": "https://untrusted.example"}), "error"),
])
def test_evidence_outcomes_and_bounds(settings, request_data, response, status):
    binding = configured_query(settings)
    calls = []

    def handler(request):
        calls.append(request)
        return response

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = ToolGateway(binding, client).execute(
            "prometheus.errors", Investigation.model_validate(request_data), time.time() + 30)
    assert result.status == status
    assert len(calls) == 1
    assert calls[0].url.params["query"] == binding.queries[0].expression
    assert calls[0].url.params["step"] == "60"
    assert "do-not-leak" not in result.model_dump_json()


def test_loki_redaction_and_truncation(settings, request_data):
    binding = configured_query(settings, "loki")
    payload = {"status": "success", "data": {"resultType": "streams", "result": [
        {"stream": {"service": "payments-api", "token": "secret"},
         "values": [["1784989930000000000", "password=secret upstream timeout"] for _ in range(100)]}
    ]}}
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as client:
        result = ToolGateway(binding, client).execute("loki.errors", Investigation.model_validate(request_data),
                                                     time.time() + 30)
    assert result.status == "partial"
    assert "secret" not in result.model_dump_json()
    assert "[REDACTED]" in result.model_dump_json()


def test_unknown_tool_never_makes_network_request(settings, request_data):
    def forbidden(_):
        pytest.fail("forbidden network request")
    with httpx.Client(transport=httpx.MockTransport(forbidden)) as client:
        result = ToolGateway(settings.bindings[0], client).execute(
            "shell.run", Investigation.model_validate(request_data), time.time() + 30)
    assert result.status == "denied"


class FakePlanner:
    def __init__(self, final="valid"):
        self.final = final

    def decide(self, request, evidence, available, deadline):
        if not evidence:
            return Decision(kind="tool", tool=available[0]), {"input_tokens": 20, "output_tokens": 10}
        if self.final == "injection":
            return Decision(kind="tool", tool="shell.run"), {}
        evidence_id = evidence[0].id if self.final == "valid" else "another-tenants-evidence"
        return Decision(kind="report", findings={
            "summary": "A recent deployment correlates with errors; verify causality.",
            "observations": [{"text": "Errors increased in the supplied snapshot.", "evidence_ids": [evidence_id]}],
            "hypotheses": [], "missing_evidence": [], "next_checks": ["Inspect traces."]
        }), {"input_tokens": 30, "output_tokens": 20}


@pytest.mark.parametrize("final", ["valid", "injection", "bad_citation"])
def test_agent_planning_citations_and_injection(settings, request_data, monkeypatch, final):
    settings.mode = "anthropic"
    settings.model = "evaluated-model-id"
    settings.bindings[0].allow_hosted_model = True
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    store, row = queued(settings, request_data)
    process_one(settings, store, planner=FakePlanner(final))
    result = store.get(row["id"], "one")
    assert result["status"] == "partial"
    assert result["model_calls"] == 2
    assert len(store.evidence(row["id"])) == 1
    assert "another-tenants-evidence" not in result["report"]
    assert "shell.run" not in result["report"]
    if final == "valid":
        assert result["input_tokens"] == 50
        assert result["output_tokens"] == 30
    else:
        assert "Model output" in result["report"]


def test_hosted_model_requires_explicit_binding_permission(client, settings, request_data):
    settings.mode = "anthropic"
    settings.model = "evaluated-model-id"
    assert submit(client, request_data).status_code == 403


def test_anthropic_wire_contract_and_input_budget(settings, request_data, monkeypatch):
    settings.model = "evaluated-model-id"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-model-key")
    calls = []

    def handler(request):
        calls.append(request)
        payload = json.loads(request.content)
        assert request.url == "https://api.anthropic.com/v1/messages"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert payload["tool_choice"]["name"] == "next_step"
        assert payload["model"] == "evaluated-model-id"
        return httpx.Response(200, json={"content": [{"type": "tool_use", "name": "next_step",
                                                     "input": {"kind": "tool", "tool": "signals.correlate"}}],
                                        "usage": {"input_tokens": 20, "output_tokens": 5}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        planner = AnthropicPlanner(settings, client)
        decision, usage = planner.decide(Investigation.model_validate(request_data), [],
                                        ["signals.correlate"], time.time() + 30)
        assert decision.tool == "signals.correlate"
        assert usage["input_tokens"] == 20
        settings.max_input_chars = 1
        with pytest.raises(ValueError, match="budget"):
            planner.decide(Investigation.model_validate(request_data), [], [], time.time() + 30)
        assert len(calls) == 1


def test_provider_failure_preserves_successful_evidence(settings, request_data):
    class FailingPlanner(FakePlanner):
        def decide(self, request, evidence, available, deadline):
            if evidence:
                raise httpx.ReadTimeout("secret=must-not-appear")
            return super().decide(request, evidence, available, deadline)
    store, row = queued(settings, request_data)
    process_one(settings, store, planner=FailingPlanner())
    result = store.get(row["id"], "one")
    assert result["status"] == "partial"
    assert len(store.evidence(row["id"])) == 1
    assert "must-not-appear" not in result["report"]


def test_redaction_before_storage(client, request_data):
    response = submit(client, {**request_data, "question": "Investigate error token=very-sensitive"})
    result = client.get(f"/v1/investigations/{response.json()['run_id']}")
    assert "very-sensitive" not in result.text


def test_sqlite_backup_can_be_restored(settings, request_data, tmp_path):
    import sqlite3
    store, row = queued(settings, request_data)
    process_one(settings, store)
    destination = tmp_path / "restored.db"
    with store.connect() as db, sqlite3.connect(destination) as restored:
        db.backup(restored)
    recovered = Store(str(destination))
    assert recovered.get(row["id"], "one")["report"] == store.get(row["id"], "one")["report"]
    assert len(recovered.evidence(row["id"])) == 1


def test_complete_live_connector_and_model_protocol_path(settings, request_data, monkeypatch):
    configured_query(settings)
    settings.mode = "anthropic"
    settings.model = "evaluated-model-id"
    settings.bindings[0].allow_hosted_model = True
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    store, row = queued(settings, request_data)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/api/v1/query_range":
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "matrix", "result": [
                {"metric": {"service": "payments-api"}, "values": [[1784989930, "0.08"]]}
            ]}})
        context = json.loads(json.loads(request.content)["messages"][0]["content"])
        if context["available_tools"]:
            decision = {"kind": "tool", "tool": "prometheus.errors"}
        else:
            decision = {"kind": "report", "findings": {
                "summary": "The error-rate query returned 0.08; causality needs more evidence.",
                "observations": [{"text": "Observed error rate 0.08.",
                                  "evidence_ids": [context["evidence"][0]["id"]]}],
                "hypotheses": [], "missing_evidence": [], "next_checks": ["Compare deployments."]}}
        return httpx.Response(200, json={"content": [{"type": "tool_use", "name": "next_step", "input": decision}],
                                        "usage": {"input_tokens": 40, "output_tokens": 20}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        process_one(settings, store, client=client)
    result = store.get(row["id"], "one")
    assert result["status"] == "completed"
    assert calls == ["/v1/messages", "/api/v1/query_range", "/v1/messages"]
    assert json.loads(result["report"])["model_id"] == "evaluated-model-id"
    assert json.loads(result["report"])["incident_resolved"] is None


def test_model_budget_survives_crash_before_provider_response(settings, request_data, monkeypatch):
    settings.mode = "anthropic"
    settings.model = "evaluated-model-id"
    settings.bindings[0].allow_hosted_model = True
    settings.max_model_calls = 1
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    store, row = queued(settings, request_data)
    first = store.claim(120)
    assert store.reserve(first, 8, 1, model=True)
    with store.connect() as db:
        db.execute("UPDATE runs SET lease_until=0 WHERE id=?", (row["id"],))

    def forbidden(_):
        pytest.fail("model budget must not reset after recovery")
    with httpx.Client(transport=httpx.MockTransport(forbidden)) as client:
        process_one(settings, store, client=client)
    result = store.get(row["id"], "one")
    assert result["status"] == "partial"
    assert result["model_calls"] == 1
    assert "budget exhausted" in result["report"]


def test_webhook_payload_conflict_not_hidden_by_normalization(settings, monkeypatch):
    settings.principals[0].pagerduty_secret_env = "PD_SECRET"
    settings.bindings[0].pagerduty_service_id = "PSERVICE"
    monkeypatch.setenv("PD_SECRET", "test-signing-secret")
    event = json.loads((EXAMPLES / "sample_alert.json").read_text())
    event["event"]["data"]["service"]["id"] = "PSERVICE"
    path = "/v1/integrations/pagerduty/TEST_TOKEN/events"

    def send(client):
        body = json.dumps(event).encode()
        signature = "v1=" + hmac.new(b"test-signing-secret", body, hashlib.sha256).hexdigest()
        return client.post(path, content=body, headers={"X-PagerDuty-Signature": signature})

    with TestClient(create_app(settings)) as client:
        assert send(client).status_code == 202
        event["event"]["data"]["body"]["details"] = "Changed field not used in normalized question"
        assert send(client).status_code == 409


def test_tool_timeout_remains_error_not_empty(settings, request_data):
    binding = configured_query(settings)

    def timeout(_):
        raise httpx.ReadTimeout("secret=never-persist")
    with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
        evidence = ToolGateway(binding, client).execute(
            "prometheus.errors", Investigation.model_validate(request_data), time.time() + 20)
    assert evidence.status == "error"
    assert evidence.data == {}
    assert "never-persist" not in evidence.model_dump_json()


def test_nonfinite_metrics_are_not_reported_as_healthy(settings, request_data):
    binding = configured_query(settings)
    payload = {"status": "success", "data": {"resultType": "matrix", "result": [
        {"metric": {}, "values": [[1784989930, "NaN"]]}
    ]}}
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as client:
        evidence = ToolGateway(binding, client).execute(
            "prometheus.errors", Investigation.model_validate(request_data), time.time() + 20)
    assert evidence.status == "error"
    assert evidence.data == {}
