"""Render a TriageResult for Slack (Block Kit) or the terminal.

The Slack message is what the responder actually sees in the incident channel:
the likely cause up top, the evidence, the suggested actions as buttons, and
the SLO status. Action buttons are proposals — clicking routes to a human
confirmation, never a blind auto-remediation.
"""
from __future__ import annotations

from .models import TriageResult

_SEV_EMOJI = {"SEV1": "🔴", "SEV2": "🟠", "SEV3": "🟡"}


def to_blocks(r: TriageResult) -> list[dict]:
    top = r.hypotheses[0]
    sev = _SEV_EMOJI.get(r.alert.severity.value, "⚪")
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text",
         "text": f"{sev} {r.alert.severity.value}: {r.alert.title}"}},
        {"type": "section", "text": {"type": "mrkdwn",
         "text": f"*Likely root cause* ({top.confidence:.0%}): {top.summary}\n"
                 f"_Est. MTTR saved: ~{r.estimated_mttr_saved_min:.0f} min_"}},
        {"type": "section", "text": {"type": "mrkdwn",
         "text": "*Evidence*\n" + "\n".join(f"• {e}" for e in top.evidence)}},
    ]

    if r.resolution:
        steps = "\n".join(
            f"{i}. *{s.action}* — {s.detail}" + (f"\n   `{s.command}`" if s.command else "")
            for i, s in enumerate(r.resolution, 1)
        )
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Suggested steps*\n{steps}"}})

    if r.slo:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": f"*SLO* `{r.slo.name}` — budget {r.slo.error_budget_remaining_pct:.0f}% left, "
                    f"burn *{r.slo.burn_rate:.0f}×* ({r.slo.priority})"}})

    # Proposed actions — each routes to a human confirmation, not auto-exec.
    actions = [{"type": "button", "text": {"type": "plain_text", "text": "Acknowledge"}, "value": "ack"}]
    if any(s.destructive for s in r.resolution):
        actions.append({"type": "button", "style": "danger",
                        "text": {"type": "plain_text", "text": "Roll back"}, "value": "rollback",
                        "confirm": {"title": {"type": "plain_text", "text": "Confirm rollback"},
                                    "text": {"type": "plain_text", "text": "Shift production traffic to the previous revision?"},
                                    "confirm": {"type": "plain_text", "text": "Roll back"},
                                    "deny": {"type": "plain_text", "text": "Cancel"}}})
    actions.append({"type": "button", "text": {"type": "plain_text", "text": "Escalate"}, "value": "escalate"})
    blocks.append({"type": "actions", "elements": actions})
    return blocks


def to_text(r: TriageResult) -> str:
    top = r.hypotheses[0]
    lines = [
        f"[{r.alert.severity.value}] {r.alert.title}  ({r.alert.service})",
        "=" * 72,
        f"Root cause  : {top.summary}  ({top.confidence:.0%})",
        f"MTTR saved  : ~{r.estimated_mttr_saved_min:.0f} min",
        "Evidence    :",
    ]
    lines += [f"  - {e}" for e in top.evidence]
    if len(r.hypotheses) > 1:
        lines.append("Also considered:")
        lines += [f"  - {h.summary} ({h.confidence:.0%})" for h in r.hypotheses[1:]]
    lines.append("Suggested steps:")
    for i, s in enumerate(r.resolution, 1):
        tag = "  [!] " if s.destructive else "      "
        lines.append(f"{tag}{i}. {s.action} — {s.detail}")
        if s.command:
            lines.append(f"          $ {s.command}")
        if s.caution:
            lines.append(f"          caution: {s.caution}")
    if r.slo:
        lines.append(f"SLO         : {r.slo.name} — {r.slo.error_budget_remaining_pct:.0f}% budget, "
                     f"burn {r.slo.burn_rate:.0f}× ({r.slo.priority})")
    return "\n".join(lines)
