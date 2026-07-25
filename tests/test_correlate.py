"""Correlation-rule tests — each cause is detected and discriminated."""
from incident_triage import correlate
from incident_triage.models import Cause, Dependency, Deploy, Rate, Signals


def sig(**kw) -> Signals:
    return Signals(service="svc", **kw)


def top(sig_: Signals) -> Cause:
    return correlate.correlate(sig_)[0].cause


def test_bad_deploy_detected():
    s = sig(error_rate=Rate(0.08, 0.002), deploys=[Deploy("svc", "v2", 6)])
    hyps = correlate.correlate(s)
    assert hyps[0].cause is Cause.BAD_DEPLOY
    assert hyps[0].confidence > 0.85          # recent + huge spike = high confidence


def test_deploy_without_spike_is_not_blamed():
    # a deploy with flat error rate should not be flagged as the cause
    s = sig(error_rate=Rate(0.002, 0.002), deploys=[Deploy("svc", "v2", 6)])
    assert top(s) is not Cause.BAD_DEPLOY


def test_oom_detected():
    assert top(sig(oom_killed=2, restarts=5)) is Cause.OOM


def test_saturation_detected():
    assert top(sig(cpu_util=0.95)) is Cause.SATURATION


def test_dependency_detected():
    assert top(sig(dependencies=[Dependency("orders-db", healthy=False)])) is Cause.DEPENDENCY


def test_config_change_detected_when_no_deploy():
    s = sig(config_changes=[{"what": "feature-flag checkout_v2", "minutes_ago": 5}])
    assert top(s) is Cause.CONFIG_CHANGE


def test_deploy_wins_over_config_change():
    # if there was a deploy, the config-change rule defers to it
    s = sig(error_rate=Rate(0.08, 0.002),
            deploys=[Deploy("svc", "v2", 6)],
            config_changes=[{"what": "flag", "minutes_ago": 5}])
    causes = {h.cause for h in correlate.correlate(s)}
    assert Cause.BAD_DEPLOY in causes
    assert Cause.CONFIG_CHANGE not in causes


def test_unknown_when_no_signal():
    assert top(sig()) is Cause.UNKNOWN


def test_hypotheses_ranked_by_confidence():
    s = sig(error_rate=Rate(0.08, 0.002), deploys=[Deploy("svc", "v2", 6)],
            oom_killed=1)
    confs = [h.confidence for h in correlate.correlate(s)]
    assert confs == sorted(confs, reverse=True)
