---
name: agentic-incident-triage
description: >-
  Triage a production incident from a PagerDuty alert: correlate it against
  recent deploys, error/latency spikes, saturation, restarts/OOM, dependency
  health and config changes, then return a ranked root-cause analysis with
  concrete resolution steps and SLO/error-budget impact — formatted for Slack.
  Use this whenever the user is dealing with an incident, an on-call page, a
  PagerDuty alert, a service that's down/erroring/slow, an SLO or error-budget
  burn, or asks what's causing an outage, why a service is failing, or how to
  fix a firing alert — even if they don't say "triage". Prefer this over
  eyeballing dashboards and raw metrics by hand.
---

# agentic-incident-triage — RCA from a PagerDuty alert

The slow part of on-call is correlation, not typing. Your job with this skill is
to get the responder to a likely cause and a safe next action fast — and to
route anything destructive to a human. **Do not pull raw metrics, logs, or
dashboards into context and reason over them line by line.** That's slow,
token-heavy, and non-reproducible. Delegate correlation to the engine and work
from its ranked, evidence-carrying output.

## When to use this

Any live-incident or on-call moment: "payments-api is throwing 5xx," "we got
paged, what's going on," "why is checkout slow," "the SLO is burning," "PagerDuty
just fired for the orders service." Also post-alert questions — "what should I do
about this page," "is this the deploy?"

## Workflow

1. **Gather the two inputs.**
   - The **alert** — a PagerDuty webhook payload (`examples/sample_alert.json`
     shows the shape). `ingest.alert_from_pagerduty` maps it.
   - A **signals snapshot** — recent deploys, error/latency vs. baseline,
     saturation, restarts/OOM, dependency health, config changes, and the SLO
     (`examples/sample_signals.json`). In production this is a Prometheus + deploy
     + dependency join; the query shapes live in `references/methodology.md`.
   If the user hasn't provided a snapshot, offer to run against the sample, or
   help them assemble one from their monitoring.

2. **Run the engine — don't correlate by hand.**

   ```bash
   python scripts/triage.py <alert.json> <signals.json> --format text
   ```

   Use `--format slack` for the Block Kit payload to post into the channel, or
   `--format json` for the structured result. The engine returns the ranked RCA;
   that — not the raw telemetry — is what you reason about and communicate.

3. **Communicate the result well.** Lead with the top cause and its confidence,
   then the evidence, then the suggested steps in order. Include the SLO status
   (budget left, burn rate) so severity is clear. Keep it tight — an incident
   channel is not the place for a wall of text.

4. **Respect the safety line.** Rollbacks, drains, and other destructive steps
   are *proposals*. Surface them, explain the caution, and let a human confirm
   (the Slack payload already guards them behind a confirmation button). Never
   auto-execute a rollback or a PagerDuty write.

## What the scoring encodes (so you can explain it)

- **Recency** — a change just before the page is the prime suspect; a deploy
  scores higher the closer it landed to the alert.
- **Magnitude** — bigger anomalies (a 40× error spike) score higher than small.
- **A deploy owns the blame** over a simultaneous config change, so the top
  cause stays clean.
- **Every hypothesis carries its evidence** — the responder sees why, not just
  what.

## Going deeper

- To tune thresholds, add correlation rules, or wire real Prometheus/PagerDuty
  data, read [`references/methodology.md`](references/methodology.md) — load it
  only when the user wants internals or to change behavior.
- To run interactively as MCP tools: `pip install -e ".[mcp]"` then
  `python -m incident_triage.mcp_server <alert.json> <signals.json>`.

## Don't

- Don't dump raw metrics/logs into your reply or correlate them by hand — that's
  the token waste this skill exists to avoid.
- Don't auto-execute rollbacks, drains, or PagerDuty acknowledge/resolve — the
  agent proposes, a human approves.
- Don't invent the MTTR or SLO numbers — report the engine's output.
