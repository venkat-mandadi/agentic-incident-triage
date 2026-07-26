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

**Your role.** Act as an experienced on-call SRE running triage: calm under a
page, biased toward the fastest *safe* next action, and disciplined about handing
anything destructive to a human. The engine does the correlation; you decide what
it means and what to tell the responder.

The slow part of on-call is correlation, not typing. Your job with this skill is
to get the responder to a likely cause and a safe next action fast — and to
route anything destructive to a human. **Do not pull raw metrics, logs, or
dashboards into context and reason over them line by line.** That's slow,
token-heavy, and non-reproducible. Delegate correlation to the engine and work
from its ranked, evidence-carrying output.

## What you need to run this

**The engine (required).** Python 3.10+ and the bundled `incident_triage`
package. It runs offline against a saved alert + signals snapshot — no live
connections needed for the sample. The rest is for real incidents.

**MCP servers (for live use).** Connect the tool servers that feed the
correlation and carry the result:

- **A PagerDuty / alerting MCP** — to pull the triggering alert. Opsgenie,
  Grafana OnCall, or a raw webhook payload work the same; the engine reads the
  alert shape in `examples/sample_alert.json`.
- **A Prometheus / observability MCP** — to gather the correlation signals:
  recent deploys, error and latency spikes, saturation, restarts/OOM, dependency
  health, config changes. Datadog, Dynatrace, New Relic, Chronosphere — any of
  them; the engine consumes the signal schema in `examples/sample_signals.json`.
- **A Slack / chat MCP** — to post the ranked RCA into the incident channel.
  Teams, Discord, and Mattermost are drop-in.
- **A Kubernetes / Argo Rollouts MCP** — optional, and only to *suggest* the
  rollback command (e.g. `kubectl argo rollouts undo`). A human runs it; the
  skill never executes a destructive action.

The engine doesn't care which vendors these are — it works from the two example
schemas. Swap any server for the equivalent you already run.

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
