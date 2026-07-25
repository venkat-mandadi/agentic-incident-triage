"""Runnable demo — no Slack or PagerDuty needed.

    python examples/run_triage.py

Loads the sample alert + signals, runs triage, and prints the terminal view
plus the Slack Block Kit JSON the bot would post.
"""
import json
from pathlib import Path

from incident_triage import ingest, slack, triage

HERE = Path(__file__).parent


def main() -> None:
    alert = ingest.alert_from_pagerduty(json.loads((HERE / "sample_alert.json").read_text()))
    sig = ingest.signals_from_dict(json.loads((HERE / "sample_signals.json").read_text()))

    result = triage.triage(alert, sig)

    print(slack.to_text(result))
    print("\n--- Slack Block Kit payload ---")
    print(json.dumps(slack.to_blocks(result), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
