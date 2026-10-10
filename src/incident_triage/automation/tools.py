"""Read-only tools with operator-selected queries and bounded network responses."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import uuid
from pathlib import Path

import httpx

from incident_triage import correlate, ingest

from .config import Binding, Query
from .contracts import Evidence, Investigation

MAX_BYTES = 256_000
SECRET = re.compile(r"(?i)\b(authorization|password|secret|api[_-]?key|token)\b")
INLINE_SECRET = re.compile(
    r"(?i)(\b(?:authorization|password|secret|api[_-]?key|token)\s*[=:]\s*)(?:Bearer\s+)?[^\s,;]+"
)


def redact(value):
    """Best-effort redaction, not a guarantee against arbitrary sensitive content."""
    if isinstance(value, dict):
        return {k: "[REDACTED]" if SECRET.search(k) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return INLINE_SECRET.sub(r"\1[REDACTED]", value)
    return value


def request_json(client, method, url, deadline, **kwargs):
    remaining = deadline - time.time()
    if remaining <= 0:
        raise TimeoutError()
    with client.stream(method, url, timeout=min(15, remaining), follow_redirects=False, **kwargs) as response:
        response.raise_for_status()
        body = bytearray()
        for chunk in response.iter_bytes():
            if time.time() >= deadline:
                raise TimeoutError()
            body.extend(chunk)
            if len(body) > MAX_BYTES:
                raise ValueError("response exceeds byte limit")
    result = json.loads(body)
    if not isinstance(result, dict):
        raise ValueError("expected response object")
    return result


class ToolGateway:
    def __init__(self, binding: Binding, client: httpx.Client):
        self.binding = binding
        self.client = client

    @property
    def names(self):
        return (["signals.correlate"] if self.binding.snapshot_path else []) + [
            f"{q.kind}.{q.name}" for q in self.binding.queries
        ]

    def execute(self, name: str, request: Investigation, deadline: float) -> Evidence:
        binding = self.binding
        if (request.service, request.environment) != (binding.service, binding.environment):
            raise PermissionError("tool scope mismatch")
        evidence = Evidence(id=str(uuid.uuid4()), tool=name, status="error", window=request.window,
                            scope={"tenant": binding.tenant, "service": binding.service,
                                   "environment": binding.environment})
        if name not in self.names:
            evidence.status = "denied"
            evidence.warnings = ["tool is not allowlisted"]
            return evidence
        try:
            if name == "signals.correlate":
                self.snapshot(evidence)
            else:
                query = next(q for q in binding.queries if f"{q.kind}.{q.name}" == name)
                self.query(query, request, evidence, deadline)
        except httpx.HTTPStatusError as exc:
            evidence.status = "denied" if exc.response.status_code in {401, 403} else "error"
            evidence.warnings = [f"upstream HTTP {exc.response.status_code}"]
        except (httpx.HTTPError, TimeoutError):
            evidence.warnings = ["upstream unavailable or timed out"]
        except (OSError, ValueError, TypeError, KeyError, OverflowError, AttributeError):
            evidence.warnings = ["invalid, oversized, or unavailable evidence"]
        if evidence.status in {"error", "denied"}:
            evidence.data = {}
        evidence.data = redact(evidence.data)
        encoded = json.dumps(evidence.data, sort_keys=True, allow_nan=False)
        evidence.content_hash = hashlib.sha256(encoded.encode()).hexdigest()
        return evidence

    def snapshot(self, evidence):
        path = Path(self.binding.snapshot_path)
        with path.open("rb") as source:
            raw = source.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("snapshot too large")
        data = json.loads(raw)
        if data.get("service") != self.binding.service:
            raise ValueError("snapshot service mismatch")
        # Snapshot is an operator-attested fixture, never a claim of live telemetry.
        for key in ("cpu_util", "mem_util"):
            if key in data and (not isinstance(data[key], (float, int)) or not 0 <= data[key] <= 1):
                raise ValueError("invalid utilization")
        for key in ("error_rate", "latency_p99_ms"):
            if data.get(key):
                for value in data[key].values():
                    if not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
                        raise ValueError("invalid rate")
        for change in data.get("deploys", []) + data.get("config_changes", []):
            age = change["minutes_ago"]
            if not isinstance(age, (float, int)) or not math.isfinite(age) or age < 0:
                raise ValueError("invalid change age")
        hypotheses = correlate.correlate(ingest.signals_from_dict(data))
        evidence.status = "partial"
        evidence.query = "operator-configured snapshot correlation"
        evidence.warnings = ["Snapshot is not live telemetry; freshness and time-window alignment require review."]
        evidence.data = {"hypotheses": [
            {"cause": h.cause.value, "claim": h.summary, "observations": h.evidence}
            for h in hypotheses if h.cause.value != "unknown"
        ]}

    def query(self, query: Query, request, evidence, deadline):
        headers = {}
        if query.token_env:
            secret = os.environ.get(query.token_env)
            if not secret:
                raise ValueError("missing connector credential")
            headers["Authorization"] = "Bearer " + secret
        if query.tenant_header:
            headers["X-Scope-OrgID"] = query.tenant_header
        params = {"query": query.expression, "start": request.window.start.isoformat(),
                  "end": request.window.end.isoformat()}
        if query.kind == "prometheus":
            path = "/api/v1/query_range"
            params.update(step="60", timeout="10s", limit="20")
        else:
            path = "/loki/api/v1/query_range"
            params.update(limit="100", direction="backward")
        evidence.query = query.expression
        payload = request_json(self.client, "GET", query.base_url + path, deadline,
                               params=params, headers=headers)
        if payload.get("status") != "success":
            raise ValueError("upstream reported failure")
        data = payload["data"]
        expected = "matrix" if query.kind == "prometheus" else "streams"
        if data.get("resultType") != expected or not isinstance(data.get("result"), list):
            raise ValueError("wrong response schema")
        rows = data["result"]
        limit = 20 if query.kind == "prometheus" else 100
        kept = []
        count = 0
        for row in rows[:20]:
            labels_key = "metric" if query.kind == "prometheus" else "stream"
            if not isinstance(row.get(labels_key), dict) or not isinstance(row.get("values"), list):
                raise ValueError("malformed series")
            values = []
            for point in row["values"][:61 if query.kind == "prometheus" else max(0, 100 - count)]:
                if not isinstance(point, list) or len(point) != 2:
                    raise ValueError("malformed sample")
                if query.kind == "prometheus":
                    if not all(math.isfinite(float(v)) for v in point):
                        raise ValueError("nonfinite metric is unknown, not healthy")
                elif not isinstance(point[1], str) or not str(point[0]).isdigit():
                    raise ValueError("invalid log entry")
                values.append(point)
            count += len(values)
            kept.append({labels_key: row[labels_key], "values": values})
        evidence.data = {"resultType": expected, "result": kept} if count else {}
        capped = len(rows) >= 20 or (query.kind == "loki" and count >= limit)
        cropped = sum(len(r["values"]) for r in rows) > count
        evidence.status = "ok" if count else "empty"
        if payload.get("warnings") or capped or cropped:
            evidence.status = "partial"
            evidence.warnings = ["Upstream warning or result limit; evidence may be incomplete."]
