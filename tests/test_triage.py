"""End-to-end triage tests against the sample incident."""
import json
from pathlib import Path

import pytest

from incident_triage import ingest, slack, triage
from incident_triage.models import Cause

EX = Path(__file__).parent.parent / "examples"


@pytest.fixture
def result():
    alert = ingest.alert_from_pagerduty(json.loads((EX / "sample_alert.json").read_text()))
    sig = ingest.signals_from_dict(json.loads((EX / "sample_signals.json").read_text()))
    return triage.triage(alert, sig)


def test_alert_parsed(result):
    assert result.alert.service == "payments-api"
    assert result.alert.severity.value == "SEV1"      # high urgency


def test_top_cause_is_bad_deploy(result):
    assert result.hypotheses[0].cause is Cause.BAD_DEPLOY


def test_resolution_leads_with_rollback(result):
    first = result.resolution[0]
    assert "roll back" in first.action.lower()
    assert "argo rollouts undo payments-api" in first.command
    assert first.destructive is True
    assert first.caution                                # destructive steps must warn


def test_mttr_saved_is_the_headline_number(result):
    assert result.estimated_mttr_saved_min == pytest.approx(17.0)   # 20 -> 3


def test_slo_flags_fast_burn(result):
    assert result.slo is not None
    assert result.slo.priority == "fast-burn"           # burn_rate 14x
    assert result.slo.minutes_to_exhaustion is not None


def test_summary_is_actionable(result):
    assert "root cause" in result.summary.lower()
    assert "first action" in result.summary.lower()


def test_slack_blocks_render(result):
    blocks = slack.to_blocks(result)
    assert blocks[0]["type"] == "header"
    actions = [b for b in blocks if b["type"] == "actions"]
    assert actions, "expected an actions block with buttons"
    buttons = actions[0]["elements"]
    assert any(b.get("style") == "danger" for b in buttons)   # rollback is guarded


def test_text_render(result):
    txt = slack.to_text(result)
    assert "Root cause" in txt and "MTTR saved" in txt


def test_result_serializes(result):
    d = result.as_dict()
    assert d["hypotheses"][0]["cause"] == "bad_deploy"
    assert d["resolution"][0]["destructive"] is True
