"""Run the API or worker, or exercise the complete local path without credentials."""
from __future__ import annotations

import argparse
import json
import signal
import tempfile
import threading
from pathlib import Path

from .config import Settings, load_settings
from .contracts import Investigation
from .store import Store
from .worker import process_one


def main(argv=None):
    parser = argparse.ArgumentParser(prog="triage-agent")
    commands = parser.add_subparsers(dest="command", required=True)
    api = commands.add_parser("serve", help="Start authenticated intake; run a worker separately")
    api.add_argument("--config", required=True)
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", default=8080, type=int)
    worker = commands.add_parser("worker", help="Process durable investigations")
    worker.add_argument("--config", required=True)
    worker.add_argument("--once", action="store_true")
    demo = commands.add_parser("demo", help="Offline synthetic run in a temporary database")
    demo.add_argument("--snapshot", default="examples/sample_signals.json")
    args = parser.parse_args(argv)
    if args.command == "demo":
        with tempfile.TemporaryDirectory(prefix="triage-demo-") as directory:
            settings = Settings(database=str(Path(directory) / "triage.db"),
                                principals=[{"token_env": "DEMO_UNUSED", "tenant": "demo",
                                             "services": ["payments-api"], "environments": ["demo"]}],
                                bindings=[{"tenant": "demo", "service": "payments-api", "environment": "demo",
                                           "snapshot_path": str(Path(args.snapshot).resolve())}])
            request = Investigation(service="payments-api", environment="demo", question="Why are requests failing?",
                                    window={"start": "2026-07-25T14:17:10Z", "end": "2026-07-25T14:32:10Z"})
            store = Store(settings.database)
            run, _ = store.submit("demo", "DEMO_UNUSED", "demo", request, settings.revision)
            process_one(settings, store)
            row = store.get(run["id"], "demo")
            print(json.dumps({"status": row["status"], "report": json.loads(row["report"]) if row["report"] else None,
                              "evidence": [e.model_dump(mode="json") for e in store.evidence(run["id"])]}, indent=2))
            return 0 if row["status"] in {"completed", "partial"} else 1
    settings = load_settings(args.config)
    if args.command == "serve":
        import uvicorn

        from .api import create_app
        uvicorn.run(create_app(settings), host=args.host, port=args.port, access_log=False)
        return 0
    store = Store(settings.database)
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    while not stop.is_set():
        worked = process_one(settings, store)
        if args.once:
            break
        if not worked:
            stop.wait(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
