"""Orchestrate one triage: Alert + Signals -> ranked RCA + resolution + SLO."""
from __future__ import annotations

from . import correlate, runbook, slo
from .models import Alert, Signals, TriageResult


def triage(alert: Alert, sig: Signals, cfg: correlate.Config = correlate.DEFAULT) -> TriageResult:
    hypotheses = correlate.correlate(sig, cfg)
    top = hypotheses[0]
    steps = runbook.steps_for(top, sig)
    impact = slo.slo_impact(sig)
    saved = slo.mttr_saved(top.confidence)

    first = steps[0].action.lower() if steps else "investigate"
    slo_note = ""
    if impact:
        slo_note = f" SLO '{impact.name}' burn {impact.burn_rate:.0f}× ({impact.priority})."
    summary = (
        f"Likely root cause: {top.summary} (confidence {top.confidence:.0%}). "
        f"Suggested first action: {first}.{slo_note}"
    )

    return TriageResult(
        alert=alert,
        hypotheses=hypotheses,
        resolution=steps,
        slo=impact,
        estimated_mttr_saved_min=saved,
        summary=summary,
    )
