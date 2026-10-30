"""Query bounds, LogQL scoping, connector error handling and credential references."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from moks.connectors.base import ConnectorError, resolve_credential
from moks.connectors.loki import LokiConnector, selector_namespaces
from moks.connectors.prometheus import PrometheusConnector, auto_step
from moks.models import Environment
from moks.rbac import Binding, Principal
from moks.scope import Scope, ScopeError
from moks.tools.builtin import LokiQueryArgs, LokiSearchArgs, build_logql, loki_query
from moks.tools.contracts import ToolContext
from moks.tools.executor import truncate_json

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


def ctx(window_minutes=60, max_minutes=360, cluster_wide=False, connectors=None):
    scope = Scope("prod", ("*",) if cluster_wide else ("shop",), ("*",), ())
    return ToolContext(None, Environment(name="prod", kind="eks"), scope, Principal("u", "u@x", [Binding("investigator")]),
                       NOW - timedelta(minutes=window_minutes), NOW, connectors or {}, timedelta(minutes=max_minutes))


def test_window_is_clamped_to_environment_maximum():
    c = ctx(window_minutes=10_000, max_minutes=360)
    start, end = c.window(None)
    assert end - start == timedelta(minutes=360)
    start, end = c.window(15)
    assert end - start == timedelta(minutes=15)
    start, _ = c.window(100_000)  # asking for more than the window never widens it
    assert NOW - start == timedelta(minutes=360)


def test_auto_step_bounds_points():
    for minutes in (5, 60, 360, 10080):
        step = auto_step(NOW - timedelta(minutes=minutes), NOW)
        assert minutes * 60 / step <= 130


def test_logql_selector_namespace_pinning():
    assert selector_namespaces('{namespace="shop", pod=~"a.*"} |= "x"') == ["shop"]
    assert selector_namespaces('{pod="a"}') == [None]
    assert selector_namespaces('{namespace=~"shop|secure"}') == [None]  # regex is not a pin
    assert selector_namespaces('sum(rate({namespace="shop"}[5m])) / sum(rate({namespace="secure"}[5m]))') == ["shop", "secure"]
    assert selector_namespaces('{namespace="shop"} |= "{namespace=\\"secure\\"}"') == ["shop"]  # braces in line filters ignored
    with pytest.raises(ConnectorError):
        selector_namespaces("no selector")


async def test_raw_logql_outside_scope_is_rejected():
    class L:
        namespace_label = "namespace"

    c = ctx(connectors={"loki": (None, L())})
    for q in ('{namespace="secure"}', '{pod="x"}', '{namespace=~".+"}', '{namespace="shop"} or {namespace="secure"}'):
        with pytest.raises(ScopeError):
            await loki_query(c, LokiQueryArgs(logql=q))


def test_structured_log_search_escapes_user_text():
    q = build_logql(LokiSearchArgs(namespace="shop", contains='"} |= "x" or {namespace="secure'), "namespace")
    assert q.startswith('{namespace="shop"}')
    assert selector_namespaces(q) == ["shop"]


def test_truncate_json_bounds_size():
    data = {"lines": ["x" * 200] * 1000, "meta": {"a": 1}}
    out, truncated = truncate_json(data, 5000)
    import json

    assert truncated and len(json.dumps(out)) <= 6000 and out["meta"] == {"a": 1}


# ---- connector failure handling (contract tests with a mock transport) -------------------------

def prom(handler, **cfg):
    return PrometheusConnector({"base_url": "http://prom/prometheus", "timeout_seconds": 1, "retries": 1, **cfg},
                               transport=httpx.MockTransport(handler))


async def test_prometheus_success_shapes_and_bounds():
    seen = {}

    def h(req: httpx.Request):
        seen["path"] = req.url.path
        seen["body"] = req.content.decode()
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "matrix", "result": [
            {"metric": {"pod": f"p{i}"}, "values": [[1, "1"], [2, "NaN"]]} for i in range(40)]}})

    r = await prom(h).range("up", NOW - timedelta(hours=1), NOW, 60)
    assert seen["path"] == "/prometheus/api/v1/query_range" and "query=up" in seen["body"]
    assert len(r["series"]) == 25 and r["series"][0]["values"][1][1] is None  # capped series, NaN -> null


async def test_prometheus_errors_are_classified_and_retried():
    calls = {"n": 0}

    def flaky(req):
        calls["n"] += 1
        return httpx.Response(503, text="overloaded")

    with pytest.raises(ConnectorError) as e:
        await prom(flaky).instant("up", NOW)
    assert e.value.kind == "unavailable" and calls["n"] == 2  # one retry

    with pytest.raises(ConnectorError) as e:
        await prom(lambda r: httpx.Response(401)).instant("up", NOW)
    assert e.value.kind == "auth"

    with pytest.raises(ConnectorError) as e:
        await prom(lambda r: httpx.Response(404)).instant("up", NOW)
    assert "path prefix" in e.value.message

    with pytest.raises(ConnectorError) as e:
        await prom(lambda r: httpx.Response(400, json={"status": "error", "error": "parse error at char 3"})).instant("up(", NOW)
    assert e.value.kind == "bad_request" and "parse error" in e.value.message

    def timeout(req):
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(ConnectorError) as e:
        await prom(timeout).instant("up", NOW)
    assert e.value.kind == "timeout"

    big = httpx.Response(200, content=b"x" * 2_000_000)
    with pytest.raises(ConnectorError) as e:
        c = prom(lambda r: big)
        c.http.limits.max_response_bytes = 1_000_000
        await c.instant("up", NOW)
    assert e.value.kind == "too_large"


async def test_prometheus_rejects_unbounded_ranges():
    with pytest.raises(ConnectorError):
        await prom(lambda r: httpx.Response(200)).range("up", NOW - timedelta(days=30), NOW, 15)


async def test_loki_query_shapes_lines():
    def h(req):
        assert req.url.params["limit"] == "500"  # clamped
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "streams", "result": [
            {"stream": {"namespace": "shop", "pod": "a"}, "values": [["1761818400000000000", "error A"], ["1761818401000000000", "x" * 5000]]}]}})

    loki = LokiConnector({"base_url": "http://loki:3100"}, transport=httpx.MockTransport(h))
    r = await loki.query_range('{namespace="shop"}', NOW - timedelta(hours=1), NOW, limit=10_000)
    assert r["lines"][0]["line"].endswith("…") and r["lines"][1]["line"] == "error A"


async def test_unreachable_connector_message():
    loki = LokiConnector({"base_url": "http://127.0.0.1:9", "timeout_seconds": 1, "retries": 0})
    with pytest.raises(ConnectorError) as e:
        await loki.labels(NOW - timedelta(hours=1), NOW)
    assert e.value.kind == "unavailable" and "cannot reach loki" in e.value.message


def test_credential_references(monkeypatch, tmp_path):
    import moks.connectors.base as base

    monkeypatch.setenv("MOKS_CRED_X", "v")
    assert resolve_credential({"type": "env", "name": "MOKS_CRED_X"}) == "v"
    with pytest.raises(ConnectorError):
        resolve_credential({"type": "env", "name": "HOME"})  # only MOKS_CRED_* may be read
    monkeypatch.setattr(base, "CREDENTIALS_DIR", tmp_path)
    (tmp_path / "tok").write_text("abc\n")
    assert resolve_credential({"type": "file", "path": "tok"}) == "abc"
    with pytest.raises(ConnectorError):
        resolve_credential({"type": "file", "path": "../../etc/passwd"})


def test_admin_api_refuses_inline_secrets():
    from fastapi import HTTPException

    from moks.api.admin import CredentialRef, _check_no_secrets

    with pytest.raises(HTTPException):
        _check_no_secrets({"base_url": "x", "api_token": "abc"})
    with pytest.raises(ValueError):
        CredentialRef(type="file", path="../x")


async def test_raw_logql_cannot_reach_denied_namespaces_even_for_wide_users():
    class L:
        namespace_label = "namespace"

    c = ctx(connectors={"loki": (None, L())})
    c.scope = Scope("prod", ("*",), ("*",), ("kube-system",))
    for q in ('{namespace="kube-system"}', '{pod="coredns"}'):
        with pytest.raises(ScopeError):
            await loki_query(c, LokiQueryArgs(logql=q))
