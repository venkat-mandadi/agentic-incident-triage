"""SQLite durable job queue for a single host; atomic claims and fenced leases."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager

from .contracts import Evidence, Investigation, Report

TERMINAL = {"completed", "partial", "cancelled", "failed", "timed_out"}


class Conflict(Exception):
    pass


class QueueFull(Exception):
    pass


class LostLease(Exception):
    pass


class Store:
    def __init__(self, path: str):
        self.path = path
        with self.connect() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] not in {0, 1}:
                raise ValueError("unsupported database schema version")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, tenant TEXT NOT NULL, actor TEXT NOT NULL,
                    event_key TEXT NOT NULL, payload_hash TEXT NOT NULL, request TEXT NOT NULL,
                    config_revision TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
                    created_at REAL NOT NULL, deadline REAL, lease_until REAL NOT NULL DEFAULT 0,
                    lease_token TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                    steps INTEGER NOT NULL DEFAULT 0, model_calls INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
                    report TEXT, error TEXT, UNIQUE(tenant, event_key)
                );
                CREATE INDEX IF NOT EXISTS queue ON runs(status, lease_until, created_at);
                CREATE TABLE IF NOT EXISTS evidence (
                    run_id TEXT NOT NULL REFERENCES runs(id), tool TEXT NOT NULL,
                    document TEXT NOT NULL, PRIMARY KEY(run_id, tool)
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id),
                    kind TEXT NOT NULL, created_at REAL NOT NULL
                );
                PRAGMA user_version=1;
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def event(db, run_id, kind):
        db.execute("INSERT INTO events(run_id,kind,created_at) VALUES(?,?,?)", (run_id, kind, time.time()))

    def submit(self, tenant, actor, key, request: Investigation, revision, capacity=100, payload_hash=None):
        payload = json.dumps(request.model_dump(mode="json"), sort_keys=True)
        digest = payload_hash or hashlib.sha256(payload.encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runs WHERE tenant=? AND event_key=?", (tenant, key)).fetchone()
            if row:
                if row["payload_hash"] != digest:
                    raise Conflict("idempotency key already has a different payload")
                return dict(row), False
            count = db.execute(
                "SELECT count(*) FROM runs WHERE tenant=? AND status IN ('queued','running')", (tenant,)
            ).fetchone()[0]
            if count >= capacity:
                raise QueueFull()
            run_id = str(uuid.uuid4())
            db.execute("""INSERT INTO runs
                (id,tenant,actor,event_key,payload_hash,request,config_revision,created_at)
                VALUES(?,?,?,?,?,?,?,?)""", (run_id, tenant, actor, key, digest, payload, revision, time.time()))
            self.event(db, run_id, "queued")
            return dict(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()), True

    def get(self, run_id, tenant):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=? AND tenant=?", (run_id, tenant)).fetchone()
            return dict(row) if row else None

    def claim(self, run_seconds, max_attempts=3, lease_seconds=15):
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT * FROM runs WHERE status='queued'
                OR (status='running' AND lease_until<?) ORDER BY created_at LIMIT 1""", (now,)).fetchone()
            if not row:
                return None
            token = str(uuid.uuid4())
            exhausted = row["attempts"] >= max_attempts
            expired = row["deadline"] is not None and row["deadline"] <= now
            if exhausted or expired:
                status = "timed_out" if expired else "failed"
                db.execute("UPDATE runs SET status=?,error=? WHERE id=?",
                           (status, "recovery budget exhausted", row["id"]))
                self.event(db, row["id"], status)
                return {"terminal": True}
            db.execute("""UPDATE runs SET status='running',lease_token=?,lease_until=?,
                attempts=attempts+1,deadline=COALESCE(deadline,?) WHERE id=?""",
                       (token, now + lease_seconds, now + run_seconds, row["id"]))
            self.event(db, row["id"], "running")
            return dict(db.execute("SELECT * FROM runs WHERE id=?", (row["id"],)).fetchone())

    def guard(self, db, run):
        row = db.execute("SELECT * FROM runs WHERE id=?", (run["id"],)).fetchone()
        if row["status"] != "running" or row["lease_token"] != run["lease_token"]:
            raise LostLease()
        if row["lease_until"] <= time.time():
            raise LostLease()
        return row

    def heartbeat(self, run, seconds=15):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.guard(db, run)
            db.execute("UPDATE runs SET lease_until=? WHERE id=?", (time.time() + seconds, run["id"]))

    def reserve(self, run, max_steps, max_model_calls, model=False):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.guard(db, run)
            if row["deadline"] <= time.time():
                raise TimeoutError()
            if row["steps"] >= max_steps or (model and row["model_calls"] >= max_model_calls):
                return False
            db.execute("UPDATE runs SET steps=steps+1, model_calls=model_calls+? WHERE id=?",
                       (int(model), run["id"]))
            self.event(db, run["id"], "model_requested" if model else "step_requested")
            return True

    def usage(self, run, input_tokens, output_tokens):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.guard(db, run)
            db.execute("UPDATE runs SET input_tokens=input_tokens+?,output_tokens=output_tokens+? WHERE id=?",
                       (max(0, input_tokens), max(0, output_tokens), run["id"]))

    def evidence(self, run_id) -> list[Evidence]:
        with self.connect() as db:
            return [Evidence.model_validate_json(r[0]) for r in db.execute(
                "SELECT document FROM evidence WHERE run_id=? ORDER BY rowid", (run_id,))]

    def save_evidence(self, run, evidence: Evidence):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.guard(db, run)
            if run["deadline"] <= time.time():
                raise TimeoutError()
            db.execute("INSERT INTO evidence VALUES(?,?,?)",
                       (run["id"], evidence.tool, evidence.model_dump_json()))
            self.event(db, run["id"], "evidence:" + evidence.status)

    def finish(self, run, status, report: Report | None = None, error=None):
        if status not in TERMINAL:
            raise ValueError("invalid terminal state")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.guard(db, run)
            if run["deadline"] <= time.time() and status in {"completed", "partial"}:
                raise TimeoutError()
            db.execute("UPDATE runs SET status=?,report=?,error=?,lease_until=0 WHERE id=?",
                       (status, report.model_dump_json() if report else None, error, run["id"]))
            self.event(db, run["id"], status)

    def cancel(self, run_id, tenant):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("""UPDATE runs SET status='cancelled' WHERE id=? AND tenant=?
                AND status IN ('queued','running')""", (run_id, tenant)).rowcount
            if changed:
                self.event(db, run_id, "cancelled")

    def events(self, run_id, after=0):
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT seq,kind,created_at FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT 100",
                (run_id, after))]
