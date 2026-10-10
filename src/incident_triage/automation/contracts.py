"""Versioned boundaries. Tenant identity never comes from request bodies."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import EXECUTION_VERSION, PROMPT_VERSION

def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Window(Contract):
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def bounded(self):
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("timestamps must include timezone")
        if not 0 < (self.end - self.start).total_seconds() <= 3600:
            raise ValueError("window must be positive and at most one hour")
        return self


class Investigation(Contract):
    schema_version: Literal["1.0"] = "1.0"
    service: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$")
    environment: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$")
    question: str = Field(min_length=1, max_length=2000)
    window: Window


class Evidence(Contract):
    id: str
    tool: str
    status: Literal["ok", "empty", "partial", "error", "denied"]
    observed_at: str = Field(default_factory=utcnow)
    scope: dict[str, str]
    window: Window
    query: str = ""
    content_hash: str = ""
    data: dict = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class Observation(Contract):
    text: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(min_length=1, max_length=8)


class Hypothesis(Contract):
    claim: str = Field(min_length=1, max_length=2000)
    confidence: Literal["low", "medium"] = "low"
    supporting_evidence_ids: list[str] = Field(min_length=1, max_length=8)
    contradicting_evidence_ids: list[str] = Field(default_factory=list, max_length=8)


class Findings(Contract):
    summary: str = Field(min_length=1, max_length=3000)
    observations: list[Observation] = Field(default_factory=list, max_length=12)
    hypotheses: list[Hypothesis] = Field(default_factory=list, max_length=8)
    missing_evidence: list[str] = Field(default_factory=list, max_length=20)
    next_checks: list[str] = Field(default_factory=list, max_length=10)


class Decision(Contract):
    kind: Literal["tool", "report"]
    tool: str | None = None
    findings: Findings | None = None

    @model_validator(mode="after")
    def one_action(self):
        if self.kind == "tool" and (not self.tool or self.findings is not None):
            raise ValueError("tool decision requires only a tool name")
        if self.kind == "report" and (self.findings is None or self.tool is not None):
            raise ValueError("report decision requires only findings")
        return self


class Report(Findings):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    service: str
    environment: str
    window: Window
    generated_at: str = Field(default_factory=utcnow)
    status: Literal["completed", "partial"]
    mode: Literal["deterministic", "anthropic"]
    model_id: str | None = None
    execution_version: str = EXECUTION_VERSION
    prompt_version: str = PROMPT_VERSION
    incident_resolved: None = None
    human_review_required: Literal[True] = True


def validate_citations(findings: Findings, evidence: list[Evidence]) -> None:
    """Check provenance, not semantic entailment; human review remains required."""
    usable = {e.id for e in evidence if e.status in {"ok", "partial"} and e.data}
    for item in findings.observations:
        if not set(item.evidence_ids) <= usable:
            raise ValueError("observation references unavailable evidence")
    for item in findings.hypotheses:
        refs = set(item.supporting_evidence_ids + item.contradicting_evidence_ids)
        if not refs <= usable:
            raise ValueError("hypothesis references unavailable evidence")
