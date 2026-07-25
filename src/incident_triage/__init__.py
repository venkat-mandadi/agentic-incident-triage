"""agentic-incident-triage — a Slack/PagerDuty RCA agent that cuts MTTR.

Consumes a PagerDuty alert, correlates it against an observability snapshot,
and returns a ranked root-cause analysis with resolution steps — protecting the
error budget by getting the responder to the likely cause in minutes, not tens
of minutes.

Public API:
    from incident_triage import ingest, correlate, triage, slack
"""
from . import correlate, ingest, runbook, slack, slo, triage
from .models import Alert, Cause, Hypothesis, ResolutionStep, Severity, Signals, TriageResult

__version__ = "0.1.0"

__all__ = [
    "Alert",
    "Cause",
    "Hypothesis",
    "ResolutionStep",
    "Severity",
    "Signals",
    "TriageResult",
    "correlate",
    "ingest",
    "runbook",
    "slack",
    "slo",
    "triage",
]
