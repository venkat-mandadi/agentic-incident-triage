# agentic-incident-triage

**A Slack/PagerDuty RCA agent that cuts MTTR.** It consumes a PagerDuty alert,
correlates it against a live observability snapshot — recent deploys, error and
latency spikes, saturation, restarts/OOM, dependency health, config changes —
and posts a ranked **root-cause analysis with resolution steps** into the
incident channel. The responder starts from a likely cause and a command, not a
blank dashboard.

<p>
  <img alt="CI" src="https://github.com/venkat-mandadi/agentic-incident-triage/actions/workflows/ci.yml/badge.svg">
  <img alt="Python" src="https://img.shields.io/badge/python-3.10%2B-blue">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-green">
</p>

> This is the open version of an incident-triage agent I built in production,
> where it cut **MTTR from ~20 minutes to ~3** and slowed error-budget burn
> during SLO events. The internal integrations are stripped out; the correlation
> logic and the guardrails around it are all here, and runnable.

**The split that matters: skill vs. engine.** A thin Claude **skill**
([`SKILL.md`](SKILL.md)) orchestrates — ingest the alert, run the correlation
engine, post to Slack, hand anything destructive to a human. The correlation and
scoring live in a Python **engine** (`src/incident_triage/`). So the model spends
its time on *what to say and what to recommend*, not on grinding through raw
telemetry. Faster, cheaper, and the scoring comes out the same every time.

---

## The problem

An on-call engineer's slowest step isn't fixing — it's **correlating**. The page
says "high error rate on payments-api." Now flip between the deploy log, three
Grafana dashboards, pod events, and the dependency's status page to figure out
*what changed*. That hunt is most of your MTTR, it happens while the error
budget burns, and it's the same hunt every time. It's perfect work to hand to an
agent — as long as the agent proposes and a human approves anything risky.

## What it does

1. **Ingests** a PagerDuty webhook → a typed `Alert`.
2. **Correlates** it against an observability snapshot using explainable rules —
   each ranked by *recency* (what changed just before the page) and *magnitude*
   (how big the anomaly is).
3. **Explains** the top root cause with the evidence behind it, and proposes
   **resolution steps** — rollback, scale, failover — with the actual command.
4. **Scores SLO impact** — error-budget left, burn rate, time-to-exhaustion — and
   estimates the **MTTR saved**.
5. **Posts to Slack** as Block Kit, with destructive actions (rollback) behind a
   confirmation button. The agent proposes; a human clicks.

## Quickstart

Runs offline against the bundled sample incident — no Slack or PagerDuty needed.

```bash
git clone https://github.com/venkat-mandadi/agentic-incident-triage
cd agentic-incident-triage
pip install -e ".[dev]"

python examples/run_triage.py
# or the CLI:
triage examples/sample_alert.json examples/sample_signals.json --format text
triage examples/sample_alert.json examples/sample_signals.json --format slack   # Block Kit JSON
```

### Sample output

```
[SEV1] High 5xx error rate on payments-api  (payments-api)
========================================================================
Root cause  : Recent deploy of payments-api v2.3.1 correlates with the spike  (95%)
MTTR saved  : ~17 min
Evidence    :
  - payments-api v2.3.1 deployed 6 min before the alert
  - error rate 8.0% vs 0.2% baseline (40×)
  - p99 latency 950ms vs 240ms (4.0×)
Suggested steps:
  [!] 1. Roll back the recent deploy — Revert payments-api to the last stable revision.
          $ kubectl argo rollouts undo payments-api
          caution: Confirm the previous revision is healthy; this shifts production traffic back to it.
      2. Verify recovery — Watch error rate and p99 latency return to baseline.
      3. Freeze deploys for the service — Pause the pipeline until the bad revision is understood.
SLO         : payments-api-availability — 42% budget, burn 14× (fast-burn)
```

## Running it as an agent

**As a Claude skill.** Drop the folder into your skills directory (or install
the packaged `.skill`). It triggers on incident / on-call / PagerDuty / "why is
X down" requests, runs `scripts/triage.py`, and posts the ranked RCA — never
pulling raw telemetry into the model's context. See [`SKILL.md`](SKILL.md).

**As an MCP tool:**

```bash
pip install -e ".[mcp]"
python -m incident_triage.mcp_server examples/sample_alert.json examples/sample_signals.json
```

Tools: `triage_alert()`, `top_root_cause()`, `slack_message()`.

## How the RCA scoring works

Confidence is built the way a good responder reasons:

- **Recency** — something that changed 4 minutes before the page beats something
  that changed 4 hours ago. A deploy inside the window scores higher the closer
  it is to the alert.
- **Magnitude** — a 40× error spike is more telling than a 2× one.
- **Mutual exclusion** — a deploy owns the blame over a simultaneous config
  change; idle-vs-busy style traps are avoided so the top cause is clean.

Every hypothesis carries its evidence, so the responder sees *why* it scored
what it did — "the model said so" is not an RCA.

## Safety

Right-sizing an incident response the wrong way turns one incident into two, so:

- **The agent proposes; a human approves.** Rollbacks and drains are surfaced as
  buttons behind a confirmation, never auto-executed.
- **PagerDuty writes are gated.** Parsing webhooks is pure; acknowledge/resolve
  are stubs meant to sit behind explicit human approval.

## Wiring real data

- **Alerts:** point your PagerDuty webhook at the ingestion path;
  `ingest.alert_from_pagerduty` maps the V3 event shape.
- **Signals:** build the snapshot from Prometheus queries (error rate, p99,
  saturation, restarts/OOM), your deploy history, and dependency health — map it
  into the schema in [`examples/sample_signals.json`](examples/sample_signals.json).
- **Slack:** post `slack.to_blocks(result)` to the incident channel; wire the
  button actions to your approval flow.

## Roadmap

- [ ] Live Prometheus/Grafana signal collectors (drop-in for the JSON snapshot)
- [ ] Log-pattern and trace-exemplar correlation
- [ ] Learn per-service runbooks from resolved incidents
- [ ] Post-incident timeline + draft post-mortem generation

## Tests

```bash
pytest -q      # correlation rules + scoring + Slack/text rendering
```

## License

MIT — see [LICENSE](LICENSE).
