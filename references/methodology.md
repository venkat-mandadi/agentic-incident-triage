# Methodology — correlation rules, scoring, and payloads

Load this only to explain *why* a hypothesis scored what it did, tune behavior,
or wire real data. A normal triage doesn't need it.

## Contents
- [The signal snapshot](#the-signal-snapshot)
- [Correlation rules](#correlation-rules)
- [Confidence scoring](#confidence-scoring)
- [SLO impact & MTTR saved](#slo-impact--mttr-saved)
- [Wiring real Prometheus / PagerDuty data](#wiring-real-prometheus--pagerduty-data)

## The signal snapshot

One object per incident, describing the alerting service at page time: error and
p99 latency (current vs. baseline), CPU/memory utilization, restarts and
OOM-kills, recent deploys, config changes, dependency health, node/network
state, and the SLO. Schema: [`examples/sample_signals.json`](../examples/sample_signals.json);
model: `src/incident_triage/models.py`.

## Correlation rules

In `src/incident_triage/correlate.py`. Each returns a hypothesis or nothing.

| Rule | Fires when | Cause |
| --- | --- | --- |
| `rule_bad_deploy` | a deploy for the service within the window **and** an error/latency spike | `bad_deploy` |
| `rule_oom` | OOM-kills present or restarts ≥ flag | `oom` |
| `rule_saturation` | CPU or memory utilization ≥ flag | `saturation` |
| `rule_dependency` | a dependency is unhealthy or its error rate is elevated | `dependency` |
| `rule_config_change` | a recent config change **and no deploy** (the deploy rule wins otherwise) | `config_change` |
| `rule_infra` | node pressure or network errors | `infra` |
| fallback | nothing correlated | `unknown` |

Best hypothesis per cause is kept, then all are ranked by confidence.

## Confidence scoring

Bounded to ≤ 0.97 and built from two intuitions:

- **Recency** — `(window − minutes_ago) / window`. A deploy 4 min before the page
  scores near the top of its band; one at the window edge adds little.
- **Magnitude** — for a spike, `min(1, (ratio − 1) / 9)`, so a 10× anomaly
  saturates the term; for saturation, how far utilization is past the flag.

Example (`bad_deploy`): `0.55 + recency·0.25 + magnitude·0.20`. The sample
incident — a deploy 6 min out with a 40× error spike — scores ~0.95.

Tunable knobs live in `correlate.Config` (deploy/config windows, spike ratio,
saturation and restart flags).

## SLO impact & MTTR saved

`src/incident_triage/slo.py`:

- **Error budget** — allowed downtime over a 28-day window is `(1 − target) ×
  window`; remaining budget and, at the current burn rate, an estimated
  time-to-exhaustion.
- **Priority** — `burn_rate ≥ 10` → *fast-burn* (page now); `≥ 2` → *elevated*.
- **MTTR saved** — `BASELINE_TRIAGE_MIN − AGENT_TRIAGE_MIN` (defaults 20 → 3,
  the real before/after) when the top hypothesis is confident and actionable;
  partial credit when the signal is weak. Tune the constants to your org.

## Wiring real Prometheus / PagerDuty data

- **Alert:** point the PagerDuty V3 webhook at your handler and pass
  `event` payloads to `ingest.alert_from_pagerduty`.
- **Signals:** assemble the snapshot from PromQL over your window, joined with
  deploy history and dependency health. Representative queries:
  - error rate — `sum(rate(http_requests_total{code=~"5.."}[5m])) / sum(rate(http_requests_total[5m]))`
  - p99 latency — `histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[5m])) by (le))`
  - saturation — `container_memory_working_set_bytes / kube_pod_container_resource_limits{resource="memory"}`
  - restarts / OOM — `increase(kube_pod_container_status_restarts_total[15m])`, `kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}`
- **Slack:** post `slack.to_blocks(result)`; wire the button `value`s
  (`ack` / `rollback` / `escalate`) to your approval flow.
- **PagerDuty writes:** implement the stubs in `pagerduty.py` behind explicit
  human approval.
