"""Turn external payloads into the engine's typed inputs.

Two entry points:
- ``alert_from_pagerduty`` maps a PagerDuty V3 webhook event into an ``Alert``.
- ``signals_from_dict`` maps an observability snapshot (the join of Prometheus
  queries, deploy history, and dependency health) into a ``Signals`` object.

Both accept plain dicts (parsed JSON) so the engine has no hard dependency on
any specific client library. The sample files in ``examples/`` show the exact
shapes.
"""
from __future__ import annotations

from .models import Alert, Dependency, Deploy, Rate, Severity, Signals

_URGENCY_TO_SEV = {"high": Severity.SEV1, "low": Severity.SEV3}


def alert_from_pagerduty(payload: dict) -> Alert:
    """Map a PagerDuty webhook (``event.data`` for an incident.triggered)."""
    data = payload.get("event", {}).get("data", payload)
    svc = (data.get("service") or {}).get("summary", "") or data.get("service_name", "unknown")
    sev = _URGENCY_TO_SEV.get(str(data.get("urgency", "high")).lower(), Severity.SEV2)
    return Alert(
        id=data.get("id", "unknown"),
        title=data.get("title") or data.get("summary", "Untitled incident"),
        service=svc,
        severity=sev,
        triggered_at=data.get("created_at", ""),
        description=(data.get("body") or {}).get("details", "") if isinstance(data.get("body"), dict) else "",
        source="pagerduty",
    )


def _rate(d: dict | None) -> Rate | None:
    if not d:
        return None
    return Rate(current=float(d["current"]), baseline=float(d["baseline"]))


def signals_from_dict(d: dict) -> Signals:
    return Signals(
        service=d["service"],
        error_rate=_rate(d.get("error_rate")),
        latency_p99_ms=_rate(d.get("latency_p99_ms")),
        cpu_util=float(d.get("cpu_util", 0.0)),
        mem_util=float(d.get("mem_util", 0.0)),
        restarts=int(d.get("restarts", 0)),
        oom_killed=int(d.get("oom_killed", 0)),
        deploys=[Deploy(x["service"], x["version"], float(x["minutes_ago"])) for x in d.get("deploys", [])],
        config_changes=list(d.get("config_changes", [])),
        dependencies=[Dependency(x["name"], bool(x.get("healthy", True)), float(x.get("error_rate", 0.0)))
                      for x in d.get("dependencies", [])],
        node_pressure=bool(d.get("node_pressure", False)),
        network_errors=bool(d.get("network_errors", False)),
        slo_name=d.get("slo", {}).get("name", ""),
        slo_target=float(d.get("slo", {}).get("target", 0.0)),
        error_budget_remaining_pct=float(d.get("slo", {}).get("error_budget_remaining_pct", 100.0)),
        burn_rate=float(d.get("slo", {}).get("burn_rate", 1.0)),
    )
