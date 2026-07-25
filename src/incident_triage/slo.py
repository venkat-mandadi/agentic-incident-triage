"""SLO / error-budget impact and the MTTR-saved estimate.

The point of fast triage is protecting the error budget: every minute of an
incident burns it. This module turns the raw SLO fields into a priority call
(is this a fast burn that justifies paging?) and estimates how much MTTR the
agent saved by handing the responder a ranked cause + steps instead of a blank
dashboard.
"""
from __future__ import annotations

from .models import Signals, SLOImpact

# 28-day rolling window, in minutes — the usual error-budget horizon.
_WINDOW_MIN = 28 * 24 * 60

# Baseline manual triage time vs. agent-assisted time. Tune to your org; the
# defaults reflect the real before/after on the system this is modeled on.
BASELINE_TRIAGE_MIN = 20.0
AGENT_TRIAGE_MIN = 3.0


def slo_impact(sig: Signals) -> SLOImpact | None:
    if not sig.slo_name:
        return None
    allowed_min = (1 - sig.slo_target) * _WINDOW_MIN
    remaining_min = allowed_min * sig.error_budget_remaining_pct / 100.0
    ttx = remaining_min / sig.burn_rate if sig.burn_rate > 0 else None
    if sig.burn_rate >= 10:
        priority = "fast-burn"
    elif sig.burn_rate >= 2:
        priority = "elevated"
    else:
        priority = "nominal"
    return SLOImpact(
        name=sig.slo_name,
        target=sig.slo_target,
        error_budget_remaining_pct=sig.error_budget_remaining_pct,
        burn_rate=sig.burn_rate,
        minutes_to_exhaustion=round(ttx, 1) if ttx is not None else None,
        priority=priority,
    )


def mttr_saved(top_confidence: float) -> float:
    """Minutes of MTTR the agent likely saved.

    Full credit when the top hypothesis is confident and actionable; partial
    when the signal is weak (the responder still has to dig, but starts warmer).
    """
    full = BASELINE_TRIAGE_MIN - AGENT_TRIAGE_MIN
    if top_confidence >= 0.5:
        return full
    return round(full * 0.4, 1)
