# Incident-triage automation

This implementation turns the existing correlation library into an authenticated,
durable investigation service. It implements a focused slice of the supplied
Incident Investigation Platform build guide: intake, scoped evidence collection,
bounded planning, evidence-backed reporting, cancellation, and recovery.

It does not implement automated remediation. No tool executes a shell command,
changes infrastructure, acknowledges a PagerDuty incident, or resolves an incident.
Reports always require human review.

## Quickstart

Python 3.10 or later is required. The original `triage` CLI remains dependency-free;
the API, worker, and planner use the optional `automation` dependencies.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[automation,dev]"
triage-agent demo
pytest -q
ruff check .
```

The demo creates a temporary SQLite database, queues one synthetic investigation,
runs the worker, and prints the report and evidence. It makes no network requests
and needs no credentials. Its `partial` status is intentional: a static snapshot
does not prove that current telemetry matches the requested time window.

### Start the API and worker

Use the same token and configuration in both terminals. Generate a local token
and keep it outside Git. For example, generate it with
`python -c 'import secrets; print(secrets.token_urlsafe(32))'` and set it in your
shell or secret manager. Each principal needs a distinct token of at least 32
characters. Example commands use placeholders, not working secrets.

```bash
export TRIAGE_API_TOKEN='<your-generated-token>'
triage-agent serve --config examples/automation.json --port 8080
```

In another terminal with the same environment:

```bash
triage-agent worker --config examples/automation.json
```

Submit the synthetic investigation:

```bash
curl --fail-with-body http://127.0.0.1:8080/v1/investigations \
  -H "Authorization: Bearer $TRIAGE_API_TOKEN" \
  -H 'Idempotency-Key: example-001' \
  -H 'Content-Type: application/json' \
  --data-binary @examples/investigation.json
```

The response is `202` with a `run_id` and a `Location` header. Read the result:

```bash
curl --fail-with-body "http://127.0.0.1:8080/v1/investigations/<run_id>" \
  -H "Authorization: Bearer $TRIAGE_API_TOKEN"
```

Use `triage-agent worker --config examples/automation.json --once` to process one
available job and exit. The continuous worker drains its current run on SIGTERM
or SIGINT, then stops. Set supervisor termination grace longer than `run_seconds`.

## API contract

| Endpoint | Behavior |
| --- | --- |
| `POST /v1/investigations` | Bearer authentication, required `Idempotency-Key`, durable `202` acceptance |
| `GET /v1/investigations/{id}` | Status, request, report, evidence, usage, configuration revision |
| `GET /v1/investigations/{id}/events?after=0` | Up to 100 persisted progress events and a resume cursor; polling, not SSE |
| `POST /v1/investigations/{id}/cancel` | Idempotent cancellation; fences future writes by the running worker |
| `POST /v1/integrations/pagerduty/{subscription}/events` | Signature-verified PagerDuty V3 intake |
| `GET /v1/openapi.json` | Authenticated OpenAPI schema |
| `GET /health/live` | Process health; no provider calls |
| `GET /health/ready` | Ability to acquire a database write transaction; no provider calls |

`tenant_id`, connector URLs, query text, and credentials are not request fields.
Unknown fields are rejected. The bearer token selects a tenant and permitted
services/environments from operator-owned configuration. Every run read, event
read, and cancellation checks that scope again. Errors return `401`, `403`,
`404`, `409`, `413`, `422`, or `429` as applicable; raw provider failures and
validation inputs are not echoed to clients.

Requests are limited to 256,000 bytes. Time windows require explicit timezones,
positive duration, and a maximum length of one hour. Unresolved service names
are rejected rather than triggering broad discovery.

## Configure live read-only evidence

Copy the example into `config.local.json` and set paths relative to that file.
Remove `snapshot_path` for live-only investigations. A binding can include up to
six query definitions. Queries are authored and reviewed by the operator; the
agent can only select their names. For example, inside the `payments-api` binding:

```json
{
  "queries": [
    {
      "name": "error_rate",
      "kind": "prometheus",
      "base_url": "https://prometheus.example.com",
      "token_env": "PROMETHEUS_TOKEN",
      "expression": "sum(rate(http_requests_total{service=\"payments-api\",environment=\"demo\",status=~\"5..\"}[5m])) / sum(rate(http_requests_total{service=\"payments-api\",environment=\"demo\"}[5m]))"
    },
    {
      "name": "errors",
      "kind": "loki",
      "base_url": "https://loki.example.com",
      "token_env": "LOKI_TOKEN",
      "tenant_header": "demo",
      "expression": "{service=\"payments-api\",environment=\"demo\"} |= \"error\""
    }
  ]
}
```

These are illustrative metric/label names, not universal queries. Adapt and
validate them against the authoritative source. Each expression and credential
must already restrict access to the binding's tenant/service/environment. The
application does not parse arbitrary PromQL or LogQL to prove its tenant scope.
Do not give one binding credentials that bypass upstream tenant restrictions.

Prometheus uses `/api/v1/query_range`, a 60-second step, and a 20-series limit.
Loki uses `/loki/api/v1/query_range` and a 100-line limit. Responses are capped at
256,000 bytes. Local limits also bound retained series/points. Warnings or reached
limits yield `partial`; empty results, denial, malformed responses, nonfinite
metrics, and network failure remain distinct. HTTP redirects are rejected and
environment proxy credentials are not inherited. Requests use at most a
15-second socket timeout and check the overall deadline while reading responses.

These bounds constrain requests and retained results, not the backend's total
scan cost. Configure upstream query timeouts, scan limits, and read-only roles.

Evidence stores its scope, collection time, requested window, native query,
content hash, bounded redacted result, and warnings. Static snapshot evidence is
always marked `partial` because its freshness is not independently verified.

## Enable agentic planning

The default `deterministic` mode collects all configured tools in order and
assembles a report using the original correlation rules where snapshot data is
available. It does not claim to be an LLM agent or infer causes from live metrics.

For model-driven selection and interpretation, set:

```json
{
  "mode": "anthropic",
  "model": "<your-evaluated-model-id>",
  "model_key_env": "ANTHROPIC_API_KEY"
}
```

Set `allow_hosted_model: true` on each approved binding and supply the key through
the named environment variable. The worker sends bounded, redacted question and
evidence content to `https://api.anthropic.com/v1/messages`. There is no automatic
provider fallback or implicit data transfer approval. No default model ID is
chosen; evaluate and pin a model available to your account before enabling it.

Each iteration requests a typed `next_step` decision: one available tool name or
structured findings. Tools have no model-controlled arguments. The runtime
validates the decision, rejects unknown/repeated tools, stores evidence, and
checks report citations against usable evidence in that run. API keys never
enter the model context. Logs and retrieved values are untrusted input.

Invalid decisions, fabricated citations, provider errors, missing evidence, or
exhausted budgets produce explicit partial/failure outcomes. Successfully stored
evidence survives a later provider failure. Confidence is qualitative; no
calibrated probabilities or measured MTTR savings are claimed by this path.

Citation validation proves that referenced evidence exists and is usable; it
does not prove that a claim follows from that evidence. Human review and a
representative evaluation set are still required. Redaction is best-effort and
cannot discover arbitrary sensitive content; approve the data boundary before
enabling hosted inference.

### Budgets and recovery

Defaults are 8 steps, 8 model calls, 1,500 output tokens per call, 40,000 input
characters per call, a 120-second execution deadline, 3 recovery attempts, and
100 pending/running jobs per tenant. Model token usage is recorded when a usable
provider response supplies it. Character limits are not exact input-token or
currency budgets. Use provider spending limits as well.

Runs transition from `queued` to `running`, then `completed`, `partial`, `failed`,
`cancelled`, or `timed_out`. Report completion does not mean incident recovery.
The deadline starts at first worker claim and survives restarts. Workers renew a
15-second lease every 3 seconds. Expired leases can be reclaimed; stale workers
cannot overwrite newer results. Evidence checkpoints prevent re-executing tools
whose results were committed. A crash before commit can repeat a read or model
request; this is not exactly-once external execution. Reserved steps and model
calls remain charged against the run budget after recovery.

Cancellation prevents further commits and work at the next safe boundary. An
in-flight HTTP read/model call may finish and incur cost. No mutating tools exist.
Configuration changes fail queued/recovered runs closed instead of silently
changing their scope. Submit a new idempotency key after reviewing the new
configuration. Reports preserve model, prompt, and execution versions; keep the
matching configuration revision in your deployment records.

## PagerDuty automation

Add `pagerduty_secret_env: "PAGERDUTY_WEBHOOK_SECRET"` to a principal and
`pagerduty_service_id: "<real-PagerDuty-service-ID>"` to its binding. Set the
subscription signing secret in the environment. Configure a PagerDuty V3
subscription for `incident.triggered` events, pointing to:

```text
https://<your-host>/v1/integrations/pagerduty/TRIAGE_API_TOKEN/events
```

`TRIAGE_API_TOKEN` here is the configured principal identifier (environment
variable name), never the token value. Each subscription should have a dedicated
principal/signing secret. The handler verifies HMAC-SHA256 over the unmodified
body and accepts matching `v1` signatures. The signed PagerDuty service ID must
map to exactly one permitted binding. Other event types are acknowledged as
ignored. The normalized window covers the 15 minutes ending at event occurrence.

Deduplication uses tenant, subscription identity, and **event ID**, not incident
ID. A different event for the same incident can start another investigation.
Duplicate deliveries reuse a run; conflicting payloads under the same event ID
return `409`. Keep deduplication records for the entire permitted replay period;
there is no signature timestamp freshness guarantee or automatic purge here.

PagerDuty incident writes and Slack posting are not implemented by this service.
Read reports through the authenticated API. The original Slack Block Kit renderer
and MCP snapshot tools remain available separately; their buttons are not wired
to an approval or execution backend.

## Deployment and operations

Run API and worker processes on the same host with access to the same SQLite
database and configuration. SQLite WAL is the queue and evidence store; no broker
or Temporal deployment is needed for this first version. Multiple local workers
can claim jobs safely, but keep concurrency conservative and measure contention.
Do not put this database on a network filesystem or present it as a distributed
HA deployment. Use PostgreSQL and a tested durable workflow design when multiple
hosts, HA, or throughput requirements justify that migration.

Use a restricted OS account, `umask 077`, encrypted storage, and a protected data
directory. Place HTTPS, request-rate limits, connection/body-read timeouts, and
network controls at the deployment ingress. API binds to loopback by default.
Use separate service credentials with minimum upstream permissions; restrict
worker egress to configured evidence systems and the approved model provider.
Neither the API nor the worker needs Kubernetes administration credentials.

Back up with SQLite's online backup API, including a restore drill, rather than
copying only the main file while WAL writes are active. The test suite verifies a
backup can restore reports and evidence; it does not establish production RPO or
RTO. Database schema version 1 is checked at startup. Preserve backups before
future migrations. Define retention and deletion procedures before ingesting
sensitive data; automatic retention and deletion workflows are not implemented.

Readiness checks durable intake capability, not model, connector, or worker
health. Monitor oldest queued work, worker availability, terminal errors,
provider quotas, storage growth, and queue capacity externally. No external
service is called by health probes. Rotate bearer tokens through the configured
environment and restart both processes. No SSO or dynamic membership service is
included.

## Verification and scope

Tests cover HTTP intake through worker completion; concurrent retries; signature
verification; cross-tenant/service denial; query bounds and partial/error states;
model wire contracts; unauthorized tool proposals; fabricated citations; budgets;
cancellation; lease recovery; configuration changes; provider failure; redaction;
progress replay; and database backup/restore. All provider tests use local fake
transports. No paid model call or production integration is required by CI.

Not yet implemented: a web console, live chat delivery/outbox, approval workflows,
incident writes, trace/Git/runtime connectors, knowledge ingestion, reviewed
learning, service graphs, SSO, distributed workflow infrastructure, or production
capacity/quality benchmarks. Those remain later slices of the 70-capability guide.

Primary protocol references:

- [PagerDuty webhook signatures](https://docs.pagerduty.com/developer/verifying-webhook-signatures)
- [Prometheus HTTP API](https://prometheus.io/docs/prometheus/latest/querying/api/)
- [Loki HTTP API](https://grafana.com/docs/loki/latest/reference/loki-http-api/)
- [Claude tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview)
