"""PagerDuty integration surface.

Parsing a webhook is safe and pure (see ``ingest.alert_from_pagerduty``).
*Acting* on an incident — acknowledging, resolving, adding a note — mutates
production state, so those are stubs here: the agent should propose them and a
human confirms in Slack. Wire them to the PagerDuty REST API when you're ready
to let the bot write back, ideally behind an explicit approval.
"""
from __future__ import annotations

from .ingest import alert_from_pagerduty  # re-exported for convenience

__all__ = ["acknowledge", "add_note", "alert_from_pagerduty", "resolve"]


def acknowledge(incident_id: str, api_token: str) -> None:  # pragma: no cover
    raise NotImplementedError(
        "POST /incidents/{id} status=acknowledged. Gate behind human approval — "
        "the agent proposes, a person confirms."
    )


def resolve(incident_id: str, api_token: str) -> None:  # pragma: no cover
    raise NotImplementedError("POST /incidents/{id} status=resolved. Human-approved only.")


def add_note(incident_id: str, note: str, api_token: str) -> None:  # pragma: no cover
    raise NotImplementedError("POST /incidents/{id}/notes — safe to post the RCA summary as a note.")
