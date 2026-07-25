"""Map a root-cause hypothesis to concrete, safe resolution steps.

The steps are written for a human on-call engineer to execute — the agent
*proposes*, a human *approves*. Destructive actions (rollbacks, restarts) are
flagged and carry a caution, because auto-remediation that's wrong just turns
one incident into two.
"""
from __future__ import annotations

from .models import Cause, Hypothesis, ResolutionStep, Signals

_verify = ResolutionStep(
    "Verify recovery",
    "Watch error rate and p99 latency return to baseline before resolving the page.",
)


def _bad_deploy(sig: Signals) -> list[ResolutionStep]:
    svc = sig.service
    ver = sig.deploys[0].version if sig.deploys else "previous"
    return [
        ResolutionStep(
            "Roll back the recent deploy",
            f"Revert {svc} from {ver} to the last stable revision. This is the fastest path "
            f"to recovery when a deploy correlates with the spike.",
            command=f"kubectl argo rollouts undo {svc}",
            destructive=True,
            caution="Confirm the previous revision is healthy; this shifts production traffic back to it.",
        ),
        _verify,
        ResolutionStep("Freeze deploys for the service",
                       "Pause the pipeline until the bad revision is understood, to avoid re-triggering."),
    ]


def _oom(sig: Signals) -> list[ResolutionStep]:
    return [
        ResolutionStep("Restore memory headroom",
                       "Raise the container memory limit (or add replicas) to stop the OOM-kill loop.",
                       command=f"kubectl set resources deploy/{sig.service} --limits=memory=<new>",
                       caution="Treat as stabilization, not a fix — find the leak next."),
        ResolutionStep("Investigate the growth",
                       "Compare memory/heap pre- and post-incident; capture a heap dump if it's a JVM. "
                       "Feed the workload into the right-sizing engine to set a safe request/limit."),
    ]


def _saturation(sig: Signals) -> list[ResolutionStep]:
    return [
        ResolutionStep("Add capacity",
                       "Scale out replicas or let the HPA catch up to shed the saturation.",
                       command=f"kubectl scale deploy/{sig.service} --replicas=<n+>"),
        ResolutionStep("Right-size requests/limits",
                       "If saturation is chronic, the requests are too low — right-size against p95 + headroom."),
    ]


def _dependency(sig: Signals) -> list[ResolutionStep]:
    dep = next((d.name for d in sig.dependencies if not d.healthy or d.error_rate > 0.05), "the dependency")
    return [
        ResolutionStep(f"Engage the '{dep}' owners",
                       f"The failure is downstream in {dep} — page its on-call; your service is the victim."),
        ResolutionStep("Shed load on the dependency",
                       "Enable the circuit breaker / failover / retry budget so the failure doesn't cascade."),
    ]


def _config_change(sig: Signals) -> list[ResolutionStep]:
    what = sig.config_changes[0].get("what", "the recent change") if sig.config_changes else "the recent change"
    return [
        ResolutionStep("Revert the config change",
                       f"Roll back {what} — a config/flag flip with no deploy is the prime suspect.",
                       destructive=True,
                       caution="Confirm the previous value; some flags gate data writes."),
        _verify,
    ]


def _infra(sig: Signals) -> list[ResolutionStep]:
    return [
        ResolutionStep("Isolate the bad node",
                       "Cordon and drain the node under pressure so the scheduler moves pods off it.",
                       command="kubectl cordon <node> && kubectl drain <node> --ignore-daemonsets",
                       destructive=True,
                       caution="Ensure capacity elsewhere before draining."),
        ResolutionStep("Check network path",
                       "Rule out network errors / DNS / cert expiry between the service and its peers."),
    ]


def _unknown(sig: Signals) -> list[ResolutionStep]:
    return [
        ResolutionStep("Gather correlating signals",
                       "No single cause stood out. Pull recent deploys, config changes, dependency health, "
                       "and saturation for the service and its neighbors, then re-run triage."),
    ]


_MAP = {
    Cause.BAD_DEPLOY: _bad_deploy,
    Cause.OOM: _oom,
    Cause.SATURATION: _saturation,
    Cause.DEPENDENCY: _dependency,
    Cause.CONFIG_CHANGE: _config_change,
    Cause.INFRA: _infra,
    Cause.UNKNOWN: _unknown,
}


def steps_for(top: Hypothesis, sig: Signals) -> list[ResolutionStep]:
    return _MAP[top.cause](sig)
