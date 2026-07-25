"""MCP server exposing triage as agent tools.

Lets a Claude agent take a PagerDuty alert + a signals snapshot and get back a
ranked RCA, resolution steps, and a ready-to-post Slack message — while the
correlation logic stays deterministic and tested underneath.

    python -m incident_triage.mcp_server examples/sample_alert.json examples/sample_signals.json

``mcp`` is an optional dependency (pip install "agentic-incident-triage[mcp]").
"""
from __future__ import annotations

import json
import sys

from . import ingest, slack, triage

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover
    FastMCP = None


def _run(alert_path: str, signals_path: str):
    with open(alert_path) as f:
        alert = ingest.alert_from_pagerduty(json.load(f))
    with open(signals_path) as f:
        sig = ingest.signals_from_dict(json.load(f))
    return triage.triage(alert, sig)


def build_server(alert_path: str, signals_path: str) -> FastMCP:
    if FastMCP is None:  # pragma: no cover
        raise SystemExit('The "mcp" package is required. Install: pip install "agentic-incident-triage[mcp]"')

    mcp = FastMCP("agentic-incident-triage")

    @mcp.tool()
    def triage_alert() -> dict:
        """Triage the alert: ranked root causes, resolution steps, SLO impact."""
        return _run(alert_path, signals_path).as_dict()

    @mcp.tool()
    def top_root_cause() -> dict:
        """Just the single most likely cause and its confidence."""
        top = _run(alert_path, signals_path).hypotheses[0]
        return {"cause": top.cause.value, "confidence": round(top.confidence, 2),
                "summary": top.summary, "evidence": top.evidence}

    @mcp.tool()
    def slack_message() -> list[dict]:
        """The Block Kit message to post into the incident channel."""
        return slack.to_blocks(_run(alert_path, signals_path))

    return mcp


def main() -> None:  # pragma: no cover
    if len(sys.argv) < 3:
        print("usage: python -m incident_triage.mcp_server <alert.json> <signals.json>", file=sys.stderr)
        raise SystemExit(2)
    build_server(sys.argv[1], sys.argv[2]).run()


if __name__ == "__main__":  # pragma: no cover
    main()
