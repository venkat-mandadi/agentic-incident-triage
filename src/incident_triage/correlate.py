"""Correlation rules — the heart of the RCA.

Each rule reads the alert + signal snapshot and, if it fires, returns a
``Hypothesis`` with a confidence score and the evidence behind it. Confidence
is built from two ideas that match how a good on-call engineer actually
reasons:

- **Recency** — something that changed 4 minutes before the page is a far
  better suspect than something that changed 4 hours ago.
- **Magnitude** — a 40× error spike is more telling than a 2× one.

Scores are deliberately bounded and explainable; the agent ranks by them but a
human sees exactly why each hypothesis scored what it did.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import Cause, Hypothesis, Signals


@dataclass(frozen=True)
class Config:
    deploy_window_min: float = 30.0
    config_window_min: float = 30.0
    error_spike_ratio: float = 3.0      # current/baseline to count as a spike
    saturation_flag: float = 0.85
    restart_flag: int = 3


DEFAULT = Config()


def _clamp(x: float, lo: float = 0.0, hi: float = 0.97) -> float:
    return max(lo, min(hi, x))


def rule_bad_deploy(sig: Signals, c: Config) -> Hypothesis | None:
    recent = [d for d in sig.deploys
              if d.service == sig.service and d.minutes_ago <= c.deploy_window_min]
    if not recent:
        return None
    err_ratio = sig.error_rate.ratio if sig.error_rate else 1.0
    lat_ratio = sig.latency_p99_ms.ratio if sig.latency_p99_ms else 1.0
    if err_ratio < c.error_spike_ratio and lat_ratio < 2.0:
        return None
    d = min(recent, key=lambda x: x.minutes_ago)
    recency = max(0.0, (c.deploy_window_min - d.minutes_ago) / c.deploy_window_min)
    magnitude = min(1.0, (max(err_ratio, lat_ratio) - 1) / 9.0)
    conf = _clamp(0.55 + recency * 0.25 + magnitude * 0.20)
    evidence = [f"{d.service} {d.version} deployed {d.minutes_ago:.0f} min before the alert"]
    if sig.error_rate:
        evidence.append(f"error rate {sig.error_rate.current:.1%} vs {sig.error_rate.baseline:.1%} baseline ({err_ratio:.0f}×)")
    if sig.latency_p99_ms and lat_ratio >= 1.5:
        evidence.append(f"p99 latency {sig.latency_p99_ms.current:.0f}ms vs {sig.latency_p99_ms.baseline:.0f}ms ({lat_ratio:.1f}×)")
    return Hypothesis(Cause.BAD_DEPLOY, conf,
                      f"Recent deploy of {d.service} {d.version} correlates with the spike", evidence)


def rule_oom(sig: Signals, c: Config) -> Hypothesis | None:
    if sig.oom_killed <= 0 and sig.restarts < c.restart_flag:
        return None
    conf = _clamp(0.6 + min(sig.oom_killed, 3) / 3 * 0.25 + min(sig.restarts, 10) / 10 * 0.10)
    ev = []
    if sig.oom_killed:
        ev.append(f"{sig.oom_killed} OOM-kill(s) in the window")
    if sig.restarts:
        ev.append(f"{sig.restarts} container restart(s)")
    return Hypothesis(Cause.OOM, conf, "Memory exhaustion / OOM-kills on the service", ev)


def rule_saturation(sig: Signals, c: Config) -> Hypothesis | None:
    util = max(sig.cpu_util, sig.mem_util)
    if util < c.saturation_flag:
        return None
    which = "CPU" if sig.cpu_util >= sig.mem_util else "memory"
    conf = _clamp(0.40 + (util - c.saturation_flag) / (1 - c.saturation_flag) * 0.50)
    return Hypothesis(Cause.SATURATION, conf,
                      f"Resource saturation — {which} at {util:.0%}",
                      [f"{which} utilization {util:.0%} (>{c.saturation_flag:.0%})"])


def rule_dependency(sig: Signals, c: Config) -> Hypothesis | None:
    bad = [d for d in sig.dependencies if not d.healthy or d.error_rate > 0.05]
    if not bad:
        return None
    worst = max(bad, key=lambda d: (not d.healthy, d.error_rate))
    conf = _clamp(0.70 + min(worst.error_rate, 0.2))
    ev = [f"dependency '{worst.name}' "
          + ("reporting unhealthy" if not worst.healthy else f"error rate {worst.error_rate:.1%}")]
    return Hypothesis(Cause.DEPENDENCY, conf,
                      f"Downstream dependency '{worst.name}' is degraded", ev)


def rule_config_change(sig: Signals, c: Config) -> Hypothesis | None:
    recent = [ch for ch in sig.config_changes
              if ch.get("minutes_ago", 1e9) <= c.config_window_min]
    if not recent or sig.deploys:      # if there was a deploy, that rule owns it
        return None
    ch = min(recent, key=lambda x: x.get("minutes_ago", 1e9))
    recency = max(0.0, (c.config_window_min - ch["minutes_ago"]) / c.config_window_min)
    conf = _clamp(0.50 + recency * 0.20)
    return Hypothesis(Cause.CONFIG_CHANGE, conf,
                      f"Recent config change: {ch.get('what', 'unknown')}",
                      [f"'{ch.get('what', 'unknown')}' changed {ch['minutes_ago']:.0f} min ago"])


def rule_infra(sig: Signals, c: Config) -> Hypothesis | None:
    if not (sig.node_pressure or sig.network_errors):
        return None
    ev = []
    if sig.node_pressure:
        ev.append("node resource pressure reported")
    if sig.network_errors:
        ev.append("elevated network errors")
    return Hypothesis(Cause.INFRA, 0.55, "Infrastructure-level issue (node / network)", ev)


RULES = (rule_bad_deploy, rule_oom, rule_saturation, rule_dependency, rule_config_change, rule_infra)


def correlate(sig: Signals, c: Config = DEFAULT) -> list[Hypothesis]:
    """Run every rule, keep the best hypothesis per cause, rank by confidence."""
    found: dict[Cause, Hypothesis] = {}
    for rule in RULES:
        h = rule(sig, c)
        if h and (h.cause not in found or h.confidence > found[h.cause].confidence):
            found[h.cause] = h
    ranked = sorted(found.values(), key=lambda h: h.confidence, reverse=True)
    if not ranked:
        ranked = [Hypothesis(Cause.UNKNOWN, 0.2,
                             "No single signal stands out — needs manual investigation",
                             ["no recent deploy, OOM, saturation, or dependency signal correlated"])]
    return ranked
