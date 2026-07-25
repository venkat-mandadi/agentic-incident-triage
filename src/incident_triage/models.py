"""Domain models for the incident-triage engine.

An on-call responder's slow step is *correlation* — flipping between the alert,
the deploy log, dashboards, and pod events to figure out what changed. This
engine turns that into data: an ``Alert`` plus a ``Signals`` snapshot go in,
ranked ``Hypothesis`` objects and concrete ``ResolutionStep`` objects come out.
Everything is a plain dataclass so the correlation logic stays readable and an
LLM agent can reason over compact, typed results instead of raw telemetry.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Severity(str, Enum):
    SEV1 = "SEV1"
    SEV2 = "SEV2"
    SEV3 = "SEV3"


class Cause(str, Enum):
    BAD_DEPLOY = "bad_deploy"
    OOM = "oom"
    SATURATION = "saturation"
    DEPENDENCY = "dependency"
    CONFIG_CHANGE = "config_change"
    INFRA = "infra"
    UNKNOWN = "unknown"


# ---- inputs ---------------------------------------------------------------

@dataclass(frozen=True)
class Alert:
    id: str
    title: str
    service: str
    severity: Severity
    triggered_at: str          # ISO-8601
    description: str = ""
    source: str = "pagerduty"


@dataclass(frozen=True)
class Deploy:
    service: str
    version: str
    minutes_ago: float


@dataclass(frozen=True)
class Dependency:
    name: str
    healthy: bool
    error_rate: float = 0.0


@dataclass(frozen=True)
class Rate:
    current: float
    baseline: float

    @property
    def ratio(self) -> float:
        return self.current / self.baseline if self.baseline else float("inf")


@dataclass(frozen=True)
class Signals:
    """A point-in-time snapshot of everything worth correlating against."""
    service: str
    error_rate: Rate | None = None
    latency_p99_ms: Rate | None = None
    cpu_util: float = 0.0                 # 0..1
    mem_util: float = 0.0                 # 0..1
    restarts: int = 0
    oom_killed: int = 0
    deploys: list[Deploy] = field(default_factory=list)
    config_changes: list[dict] = field(default_factory=list)  # {what, minutes_ago}
    dependencies: list[Dependency] = field(default_factory=list)
    node_pressure: bool = False
    network_errors: bool = False
    # SLO
    slo_name: str = ""
    slo_target: float = 0.0               # e.g. 0.999
    error_budget_remaining_pct: float = 100.0
    burn_rate: float = 1.0                # multiples of normal budget consumption


# ---- outputs --------------------------------------------------------------

@dataclass(frozen=True)
class Hypothesis:
    cause: Cause
    confidence: float                     # 0..1
    summary: str
    evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ResolutionStep:
    action: str
    detail: str
    command: str = ""
    destructive: bool = False
    caution: str = ""


@dataclass(frozen=True)
class SLOImpact:
    name: str
    target: float
    error_budget_remaining_pct: float
    burn_rate: float
    minutes_to_exhaustion: float | None
    priority: str                         # "fast-burn" | "elevated" | "nominal"


@dataclass(frozen=True)
class TriageResult:
    alert: Alert
    hypotheses: list[Hypothesis]
    resolution: list[ResolutionStep]
    slo: SLOImpact | None
    estimated_mttr_saved_min: float
    summary: str

    def as_dict(self) -> dict:
        return {
            "alert": {"id": self.alert.id, "title": self.alert.title,
                      "service": self.alert.service, "severity": self.alert.severity.value},
            "summary": self.summary,
            "estimated_mttr_saved_min": round(self.estimated_mttr_saved_min, 1),
            "hypotheses": [
                {"cause": h.cause.value, "confidence": round(h.confidence, 2),
                 "summary": h.summary, "evidence": h.evidence}
                for h in self.hypotheses
            ],
            "resolution": [
                {"action": s.action, "detail": s.detail, "command": s.command,
                 "destructive": s.destructive, "caution": s.caution}
                for s in self.resolution
            ],
            "slo": None if self.slo is None else {
                "name": self.slo.name, "target": self.slo.target,
                "error_budget_remaining_pct": self.slo.error_budget_remaining_pct,
                "burn_rate": self.slo.burn_rate,
                "minutes_to_exhaustion": self.slo.minutes_to_exhaustion,
                "priority": self.slo.priority,
            },
        }
