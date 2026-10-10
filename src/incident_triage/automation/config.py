"""Operator-owned connection bindings; secrets are environment references only."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from . import EXECUTION_VERSION, PROMPT_VERSION
from .contracts import Contract


class Principal(Contract):
    token_env: str
    pagerduty_secret_env: str | None = None
    tenant: str
    services: list[str] = Field(min_length=1)
    environments: list[str] = Field(min_length=1)


class Query(Contract):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    kind: Literal["prometheus", "loki"]
    base_url: str
    expression: str = Field(min_length=1, max_length=4000)
    token_env: str | None = None
    tenant_header: str | None = None

    @field_validator("base_url")
    @classmethod
    def endpoint(cls, value):
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            raise ValueError("connector needs an HTTP(S) URL without embedded credentials")
        if url.query or url.fragment:
            raise ValueError("base URL must not contain query or fragment")
        return value.rstrip("/")


class Binding(Contract):
    tenant: str
    service: str
    environment: str
    pagerduty_service_id: str | None = None
    snapshot_path: str | None = None
    queries: list[Query] = Field(default_factory=list, max_length=6)
    allow_hosted_model: bool = False

    @model_validator(mode="after")
    def names(self):
        if len({q.name for q in self.queries}) != len(self.queries):
            raise ValueError("query names must be unique within a binding")
        return self


class Settings(Contract):
    database: str = "triage.db"
    principals: list[Principal] = Field(min_length=1)
    bindings: list[Binding] = Field(min_length=1)
    mode: Literal["deterministic", "anthropic"] = "deterministic"
    model: str | None = None
    model_key_env: str = "ANTHROPIC_API_KEY"
    max_steps: int = Field(default=8, ge=1, le=16)
    max_model_calls: int = Field(default=8, ge=1, le=16)
    max_output_tokens: int = Field(default=1500, ge=256, le=4096)
    max_input_chars: int = Field(default=40000, ge=4000, le=100000)
    run_seconds: int = Field(default=120, ge=5, le=600)
    max_pending_per_tenant: int = Field(default=100, ge=1, le=1000)
    max_attempts: int = Field(default=3, ge=1, le=5)

    @model_validator(mode="after")
    def valid(self):
        keys = [(b.tenant, b.service, b.environment) for b in self.bindings]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate service binding")
        if self.mode == "anthropic" and not self.model:
            raise ValueError("set an evaluated model ID for anthropic mode")
        return self

    def binding(self, tenant: str, service: str, environment: str) -> Binding:
        for binding in self.bindings:
            if (binding.tenant, binding.service, binding.environment) == (tenant, service, environment):
                return binding
        raise PermissionError("service binding is not authorized")

    def check_secrets(self):
        values = [os.environ.get(p.token_env, "") for p in self.principals]
        if any(len(v) < 32 for v in values) or len(set(values)) != len(values):
            raise ValueError("each principal requires a distinct token of at least 32 characters")
        for principal in self.principals:
            if principal.pagerduty_secret_env and not os.environ.get(principal.pagerduty_secret_env):
                raise ValueError("PagerDuty signing secret environment variable is missing")
        if self.mode == "anthropic" and not os.environ.get(self.model_key_env):
            raise ValueError("model API key environment variable is missing")
        for binding in self.bindings:
            for query in binding.queries:
                if query.token_env and not os.environ.get(query.token_env):
                    raise ValueError("connector token environment variable is missing")

    @property
    def revision(self) -> str:
        manifest = {"settings": self.model_dump(), "execution": EXECUTION_VERSION, "prompt": PROMPT_VERSION}
        return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()


def load_settings(path: str) -> Settings:
    config_path = Path(path).resolve()
    settings = Settings.model_validate_json(config_path.read_text())
    settings.database = str((config_path.parent / settings.database).resolve())
    for binding in settings.bindings:
        if binding.snapshot_path:
            binding.snapshot_path = str((config_path.parent / binding.snapshot_path).resolve())
    settings.check_secrets()
    return settings
