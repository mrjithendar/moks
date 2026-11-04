"""A citation is not proof. These hold reports to what their evidence can actually show."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from moks.orchestrator import evidence as ev
from moks.orchestrator.report import Cause, Fact, Report, rules_report, validate_claims, verify_causes

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def item(ref, tool="k8s_triage", ok=True, summary="3 pods running", data=None, namespace="payments", age=0, denied=False, truncated=False, kind=None):
    return {"ref": ref, "tool": tool, "args": {"namespace": namespace}, "ok": ok, "summary": summary, "data": data, "denied": denied, "truncated": truncated,
            "kind": kind, "collected_at": (NOW - timedelta(seconds=age)).isoformat(), "error": None if ok else "boom"}


def ledger(*items, namespace="payments"):
    return ev.Ledger.build(list(items), namespace=namespace, now=NOW)


def check(facts=(), summary="", causes=(), led=None, findings=()):
    rep = Report(summary=summary, observed_facts=[Fact(statement=s, evidence=r) for s, r in facts], suspected_causes=list(causes), generated_by="llm")
    return validate_claims(rep, led, list(findings))


def test_a_failed_prometheus_query_cannot_prove_workloads_are_healthy():
    rep = check([("All payment workloads are healthy.", ["E1"])], "All payment workloads are healthy.",
                led=ledger(item("E1", "prom_query", ok=False, summary="prometheus error: connection refused")))
    assert rep.observed_facts == []
    assert any("All payment workloads" in m for m in rep.missing_evidence)
    assert "healthy" not in rep.summary and "could not establish" in rep.summary
    assert rep.validation["summary_sentences_removed"][0]["sentence"].startswith("All payment")


def test_a_failed_query_may_support_a_statement_about_the_failure():
    rep = check([("The Prometheus query failed with a connection error.", ["E1"])], led=ledger(item("E1", "prom_query", ok=False)))
    assert [f.kind for f in rep.observed_facts] == ["collection"]


def test_an_access_denial_only_shows_the_denial():
    led = ledger(item("E1", "k8s_list_pods", ok=False, denied=True, summary="denied: no read access"))
    assert led.entries["E1"].kind == ev.ACCESS_DENIED
    assert check([("The payments namespace has no failing pods.", ["E1"])], led=led).observed_facts == []
    assert check([("The request for pods could not be collected: access was denied.", ["E1"])], led=led).observed_facts[0].kind == "collection"


def test_a_fabricated_reference_is_recorded_and_removed():
    rep = check([("Pods are crashing.", ["E99"])], led=ledger(item("E1")))
    assert rep.observed_facts == []
    assert "E99" in rep.validation["decisions"]["dropped"][0]["reasons"][0]


def test_a_real_citation_beside_a_fabricated_one_keeps_only_the_real_one():
    rep = check([("3 pods are running.", ["E1", "E77"])], led=ledger(item("E1")))
    assert rep.observed_facts[0].evidence == ["E1"]
    assert any("E77" in q for q in rep.observed_facts[0].qualifiers)


def test_stale_evidence_is_labeled_and_cannot_show_health():
    led = ledger(item("E1", age=3600))
    current = check([("3 pods are running.", ["E1"])], led=led).observed_facts[0]
    assert any("freshness" in q for q in current.qualifiers)
    rep = check([("The workloads are healthy.", ["E1"])], "The workloads are healthy.", led=led)
    assert rep.observed_facts == [] and rep.suspected_causes[0].origin == "demoted_fact"
    assert "healthy" not in rep.summary.split("; ")[0].lower() or "beyond the evidence" in rep.summary


def test_a_finding_contradicts_a_claim_of_health():
    findings = [{"kind": "crashloop", "severity": "high", "namespace": "payments", "resources": ["pod/api-1"], "evidence": ["E2"], "title": "x"}]
    led = ledger(item("E1"), item("E2", summary="api-1 CrashLoopBackOff"))
    rep = check([("The payments workloads are healthy.", ["E1"])], "Everything is healthy.", led=led, findings=findings)
    assert rep.observed_facts == [] and rep.suspected_causes[0].contradicting == ["E2"]
    assert rep.validation["summary_sentences_removed"]


def test_conflicting_sources_are_surfaced_not_resolved():
    led = ledger(item("E1"), item("E2", "prom_alerts", summary="2 alerts firing", data=[{"alertname": "HighErrorRate"}]))
    rep = check([("3 pods are running.", ["E1"])], led=led)
    assert rep.conflicts and any("disagree" in m for m in rep.missing_evidence)
    assert any("disagrees" in q for q in rep.observed_facts[0].qualifiers)


def test_alerts_about_another_namespace_do_not_disagree_with_its_checks():
    def alerts(*labels):
        return [{"name": "ServiceDown", "active": [{"labels": l, "state": "firing"} for l in labels]}]
    other = alerts({"instance": "cart.roboshop-ai.svc.cluster.local:4107", "job": "roboshop"}, {"namespace": "roboshop-ai"})
    assert ev.conflicts(ledger(item("E1"), item("E2", "prom_alerts", summary="2 firing", data=other)), []) == []
    here = alerts({"instance": "api.payments.svc:8080"})
    assert ev.conflicts(ledger(item("E1"), item("E2", "prom_alerts", summary="1 firing", data=here)), [])
    unknown = alerts({"alertname": "NodeDown", "instance": "10.0.0.4:9100"})  # says nothing about a namespace: still counted
    assert ev.conflicts(ledger(item("E1"), item("E2", "prom_alerts", summary="1 firing", data=unknown)), [])
    pending = [{"name": "ServiceDown", "active": [{"labels": {"namespace": "payments"}, "state": "pending"}]}]
    assert ev.conflicts(ledger(item("E1"), item("E2", "prom_alerts", summary="0 firing", data=pending)), []) == []


def test_documentation_never_states_the_current_state():
    led = ledger(item("E1", "kb_search", summary="runbook: payments runs 3 replicas", kind=ev.DOCUMENTATION))
    rep = check([("Payments runs 3 replicas.", ["E1"])], led=led)
    assert rep.observed_facts == [] and rep.suspected_causes[0].origin == "demoted_fact"
    said = check([("The runbook says payments runs 3 replicas.", ["E1"])], led=led).observed_facts[0]
    assert said.kind == "documentation"


def test_historical_evidence_is_kept_as_history():
    led = ledger(item("E1", summary="3 pods running", kind=ev.HISTORICAL))
    assert check([("3 pods were running.", ["E1"])], led=led).observed_facts[0].kind == "historical"
    assert check([("Workloads are healthy.", ["E1"])], led=led).observed_facts == []


def test_a_truncated_result_cannot_show_absence():
    led = ledger(item("E1", truncated=True))
    assert led.entries["E1"].kind == ev.INCOMPLETE
    assert check([("3 pods are running.", ["E1"])], led=led).observed_facts
    assert check([("There are no errors in payments.", ["E1"])], led=led).observed_facts == []


def test_a_claim_naming_something_the_citation_never_mentions_is_a_hypothesis():
    rep = check([("checkout-api-7f9 ran out of memory.", ["E1"])], led=ledger(item("E1", summary="3 pods running")))
    assert rep.observed_facts == [] and "checkout-api-7f9" in rep.suspected_causes[0].reasoning


def test_evidence_from_another_namespace_is_out_of_scope():
    led = ledger(item("E1", namespace="other"))
    assert check([("3 pods are running.", ["E1"])], led=led).observed_facts == []


def test_a_health_claim_is_qualified_when_part_of_the_system_could_not_be_read():
    led = ledger(item("E1"), item("E2", "prom_query", ok=False))
    fact = check([("The payments pods are healthy.", ["E1"])], led=led).observed_facts[0]
    assert any("only part of the system" in q for q in fact.qualifiers)


def test_causes_lose_citations_that_cannot_support_them():
    cause = Cause(cause="Prometheus is broken", confidence="high", evidence=["E1", "E9"], contradicting=["E9"])
    rep = check(causes=[cause], led=ledger(item("E1", "prom_query", ok=False)))
    assert rep.suspected_causes[0].evidence == [] and rep.suspected_causes[0].confidence == "low" and rep.suspected_causes[0].contradicting == []


def test_a_summary_sentence_naming_nothing_collected_is_removed():
    rep = check([("3 pods are running.", ["E1"])], "3 pods are running. billing-db-0 is the problem.", led=ledger(item("E1")))
    assert "billing-db-0" not in rep.summary and "3 pods are running." in rep.summary


def test_a_summary_may_not_confirm_a_cause():
    rep = check([("3 pods are running.", ["E1"])], "The root cause is confirmed.", led=ledger(item("E1")))
    assert "confirmed" not in rep.summary.lower().replace("not confirmed", "")


def test_the_report_says_how_claims_were_checked():
    v = check([("3 pods are running.", ["E1"])], led=ledger(item("E1"))).validation
    assert v["model_assisted"] is False and "not proof" in v["method"] and v["evidence_kinds"] == {"E1": "observation"}


def test_verification_names_its_check_target_time_and_evidence():
    rep = Report()
    led = ledger(item("E1"))
    verify_causes(rep, [{"kind": "service_selector_mismatch", "title": "Service x", "evidence": ["E1"], "resources": ["service/x"], "namespace": "payments"}], led)
    v = rep.verified_findings[0]
    assert v["check"].endswith("service_selector_mismatch") and v["target"] == "service/x" and v["at"] == "2026-11-02 12:00 UTC" and v["evidence"] == ["E1"] and v["result"]


def test_observing_a_failure_does_not_verify_its_cause():
    rep = Report()
    verify_causes(rep, [{"kind": "oomkilled", "title": "api OOMKilled", "evidence": ["E1"]}], ledger(item("E1")))
    assert rep.verified_causes == [] and rep.verified_findings[0]["category"] == "observed_failure"


def test_a_failed_tool_cannot_verify_anything():
    rep = Report()
    verify_causes(rep, [{"kind": "service_selector_mismatch", "title": "x", "evidence": ["E1"]}], ledger(item("E1", ok=False)))
    assert rep.verified_findings == []


def test_model_confidence_never_verifies():
    rep = Report(suspected_causes=[Cause(cause="x", confidence="high", evidence=["E1"], verified=True)])
    verify_causes(rep, [], ledger(item("E1")))
    assert rep.suspected_causes[0].verified is False and rep.verified_findings == []


def test_silence_from_failed_checks_is_not_called_clean():
    rep = rules_report("q", [], [item("E1", "k8s_triage", ok=False)], [])
    assert "does not show the system is healthy" in rep.summary


class Cfg:
    max_output_tokens = 800
    input_price_per_mtok = Decimal("3")
    output_price_per_mtok = Decimal("15")


def investigator():
    from moks.orchestrator.engine import Investigator
    import uuid

    return Investigator(uuid.uuid4(), "w")


def test_the_report_honors_a_cap_below_the_old_floor():
    assert investigator()._capacity(Cfg, 1000, 1500, 300) == 800


def test_the_investigation_budget_limits_output():
    inv = investigator()
    inv.tokens_in = inv.settings.max_tokens_per_investigation - 1000
    assert inv._capacity(Cfg, 900, 1500, 300) == 700


def test_too_little_room_is_refused_rather_than_cut_off():
    from moks.orchestrator.engine import BudgetExceeded

    inv = investigator()
    inv.tokens_out = inv.settings.max_tokens_per_investigation - 50
    with pytest.raises(BudgetExceeded, match="output capacity"):
        inv._capacity(Cfg, 300, 1500, 300)


async def test_a_reservation_covers_what_the_provider_really_requests(monkeypatch):
    from moks.llm import budget
    from moks.llm.providers import AnthropicProvider, LLMResponse

    seen = {}

    async def reserve(cfg, est, **kw):
        seen["est"] = est
        return "r"

    async def settle(*a):
        pass

    async def metered(*a, **kw):
        return LLMResponse("{}", 1, 1, "anthropic", "m")

    class C(Cfg):
        name, model, provider, endpoint, timeout_seconds = "c", "m", "anthropic", None, 5
        max_output_tokens = 6000

    monkeypatch.setattr(budget, "reserve", reserve)
    monkeypatch.setattr(budget, "settle", settle)
    monkeypatch.setattr(budget, "complete_metered", metered)
    await budget.complete(AnthropicProvider(C), "s", [{"role": "user", "content": "u"}], json_mode=True, max_tokens=500)
    assert seen["est"] == budget.estimate(C, 2, 4000)  # the provider's floor, not the 500 asked for
    C.max_output_tokens = 1000
    assert AnthropicProvider(C).output_tokens(500) == 1000  # the configured cap beats the floor


def test_alerts_on_services_that_do_not_exist_say_so():
    """mk4, 2026-11-04: RoboShopServiceDown fired for 8 services in an empty roboshop-ai, and the report said the
    Kubernetes checks found nothing wrong. Both were true; the message hid why."""
    gone = [{"name": "RoboShopServiceDown", "active": [{"labels": {"instance": f"{n}.roboshop-ai.svc.cluster.local:4107", "job": "roboshop"}, "state": "firing"}
                                                      for n in ("cart-service", "payment-service")]}]
    empty = item("E1", data={"findings": [], "services": [], "workloads": []}, namespace="roboshop-ai")
    out = ev.conflicts(ledger(empty, item("E2", "prom_alerts", summary="2 firing", data=gone, namespace=None), namespace="roboshop-ai"), [])
    assert out[0]["subject"] == "alerts on absent services" and "cart-service, payment-service" in out[0]["detail"] and "no Service in roboshop-ai" in out[0]["detail"]
    assert "found nothing wrong" not in out[0]["detail"]
    # A Service that exists keeps the plain disagreement: then something really is unexplained
    present = item("E1", data={"findings": [], "services": [{"name": "cart-service"}], "workloads": []}, namespace="roboshop-ai")
    out = ev.conflicts(ledger(present, item("E2", "prom_alerts", summary="2 firing", data=gone, namespace=None), namespace="roboshop-ai"), [])
    assert out[0]["subject"] == "firing alerts"
