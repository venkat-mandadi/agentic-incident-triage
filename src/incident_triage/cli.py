"""Command-line entry point:

    triage <alert.json> <signals.json> [--format text|slack|json]

Runs a full triage without an MCP client or a live Slack/PagerDuty connection.
"""
from __future__ import annotations

import argparse
import json

from . import ingest, slack, triage


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="triage", description="Triage a PagerDuty alert against an observability snapshot.")
    p.add_argument("alert", help="PagerDuty webhook JSON (see examples/sample_alert.json)")
    p.add_argument("signals", help="Observability snapshot JSON (see examples/sample_signals.json)")
    p.add_argument("--format", choices=["text", "slack", "json"], default="text")
    args = p.parse_args(argv)

    with open(args.alert) as f:
        alert = ingest.alert_from_pagerduty(json.load(f))
    with open(args.signals) as f:
        sig = ingest.signals_from_dict(json.load(f))

    result = triage.triage(alert, sig)
    if args.format == "slack":
        print(json.dumps(slack.to_blocks(result), indent=2, ensure_ascii=False))
    elif args.format == "json":
        print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
    else:
        print(slack.to_text(result))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
