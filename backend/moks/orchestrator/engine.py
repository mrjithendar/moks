"""The controlled investigation loop.

question -> scope -> baseline evidence -> (LLM-planned or rule-based) follow-up evidence ->
correlation -> hypotheses -> report. Remediation is proposed only; Phase 1 executes nothing.

Bounds: tool calls, LLM rounds, wall time (MOKS_MAX_DURATION_SECONDS), query window,
result size, tokens per investigation and the LLM configuration's daily budget. Cancellation
is cooperative and immediate: a watcher cancels the run when cancel_requested is set.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update

from .. import audit
from ..config import get_settings
from ..connectors import registry
from ..db import session_scope
from ..llm.providers import LLMError, complete_metered, provider_for
from ..models import Environment, Investigation, InvestigationEvent, LLMConfig, LLMUsage, RoleBinding, User
from ..observability import INVESTIGATION_DURATION, INVESTIGATIONS, INVESTIGATIONS_RUNNING
from ..rbac import Binding, P, Principal
from ..scope import ScopeError, effective_scope
from ..tools.contracts import ToolContext
from ..tools.executor import ToolExecutor, ToolOutcome, tools_for_planner
from . import prompts
from .report import Report, parse_json_object, rules_report, validate_llm_report

log = logging.getLogger("moks.orchestrator")

TRACE_ID = re.compile(r"\b[0-9a-f]{32}\b", re.I)
CHANGE_WORDS = re.compile(r"(?i)\b(latenc\w*|slow\w*|deploy\w*|rollout|rolled|release\w*|change\w*|regress\w*|timeout\w*|degrad\w*)\b")
MAX_CALLS_PER_ROUND = 4
MAX_NAMESPACES_SWEEP = 10


class BudgetExceeded(Exception):
    pass


async def load_principal(db, user_id: uuid.UUID) -> Principal:
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise ScopeError("the requesting user no longer exists or is disabled")
    rows = (await db.execute(select(RoleBinding).where(RoleBinding.user_id == user_id))).scalars().all()
    return Principal(str(user.id), user.email, [Binding(r.role, r.environment, r.namespace) for r in rows])


async def emit(investigation_id: uuid.UUID, seq: int, type_: str, payload: dict[str, Any]) -> None:
    """Persist one progress event in its own transaction so SSE readers see it immediately."""
    async with session_scope() as db:
        db.add(InvestigationEvent(investigation_id=investigation_id, seq=seq, type=type_, payload=payload))


class Investigator:
    def __init__(self, investigation_id: uuid.UUID, worker_id: str):
        self.id = investigation_id
        self.worker_id = worker_id
        self.settings = get_settings()
        self.seq = 0
        self.ref = 0
        self.evidence: list[dict[str, Any]] = []
        self.findings: list[dict[str, Any]] = []
        self.notes: list[str] = []
        self.tool_calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost = Decimal("0")
        self.calls_seen: set[str] = set()
        self.rejected: list[dict[str, Any]] = []
        self._window: tuple[datetime, datetime] | None = None

    async def _emit(self, type_: str, payload: dict[str, Any]) -> None:
        self.seq += 1
        await emit(self.id, self.seq, type_, payload)

    def _next_ref(self) -> str:
        self.ref += 1
        return f"E{self.ref}"

    # ---- entry point ----------------------------------------------------------------------

    async def run(self) -> None:
        INVESTIGATIONS_RUNNING.inc()
        start = time.monotonic()
        mode = "unknown"
        async with session_scope() as db:
            self.seq = int((await db.execute(select(func.coalesce(func.max(InvestigationEvent.seq), 0))
                                             .where(InvestigationEvent.investigation_id == self.id))).scalar_one())
        task = asyncio.current_task()
        watcher = asyncio.create_task(self._watch(task))
        status, error = "failed", None
        try:
            async with asyncio.timeout(self.settings.max_duration_seconds):
                mode = await self._investigate()
            status = "completed"
        except asyncio.CancelledError:
            status, error = "cancelled", "cancelled by user"
            uncancel = getattr(task, "uncancel", None)
            if uncancel:
                uncancel()
        except TimeoutError:
            status, error = "failed", f"time limit of {self.settings.max_duration_seconds}s reached"
        except ScopeError as exc:
            status, error = "failed", f"not permitted: {exc}"
        except Exception as exc:
            log.exception("investigation failed", extra={"event": "investigation_failed", "investigation_id": str(self.id)})
            status, error = "failed", f"internal error ({type(exc).__name__})"
        finally:
            watcher.cancel()
            INVESTIGATIONS_RUNNING.dec()
        if status != "completed":
            # Keep the partial evidence useful: write a rule-based report of what was collected.
            partial = rules_report("", self.findings, self.evidence, self.notes + [f"Investigation {status}: {error}"])
            await self._finish(status, partial.model_dump() if self.evidence else None, error)
            await self._emit("error" if status == "failed" else "cancelled", {"message": error})
        await self._emit("done", {"status": status})
        INVESTIGATIONS.labels(status, mode).inc()
        INVESTIGATION_DURATION.labels(mode).observe(time.monotonic() - start)

    async def _watch(self, task: asyncio.Task) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                async with session_scope() as db:
                    inv = await db.get(Investigation, self.id)
                    if inv is None or inv.cancel_requested:
                        task.cancel()
                        return
                    inv.heartbeat_at = datetime.now(timezone.utc)
            except Exception:  # transient DB errors must not kill the run
                log.warning("heartbeat failed", extra={"event": "heartbeat_failed", "investigation_id": str(self.id)})

    async def _finish(self, status: str, report: dict | None, error: str | None) -> None:
        async with session_scope() as db:
            await db.execute(update(Investigation).where(Investigation.id == self.id).values(
                status=status, report=report, error=error, finished_at=datetime.now(timezone.utc),
                tool_calls=self.tool_calls, tokens_in=self.tokens_in, tokens_out=self.tokens_out, cost_usd=self.cost,
            ))
            inv = await db.get(Investigation, self.id)
            await audit.record(db, action="investigation.finish", actor_id=inv.created_by if inv else None,
                               resource_type="investigation", resource_id=str(self.id), namespace=inv.namespace if inv else None,
                               outcome=status, details={"tool_calls": self.tool_calls, "tokens_in": self.tokens_in,
                                                        "tokens_out": self.tokens_out, "cost_usd": str(self.cost), "error": error})

    # ---- the loop -------------------------------------------------------------------------

    async def _investigate(self) -> str:
        async with session_scope() as db:
            inv = await db.get(Investigation, self.id)
            env = await db.get(Environment, inv.environment_id)
            principal = await load_principal(db, inv.created_by)
            # Re-check authorization at execution time, not just when the request was accepted.
            scope = effective_scope(principal, env, P.INVESTIGATION_RUN)
            if inv.namespace != "*":
                scope.require(inv.namespace)
            connectors, conn_errors = await registry.for_environment(db, env.id)
            llm_cfg = (await db.execute(select(LLMConfig).where(LLMConfig.purpose == "chat", LLMConfig.is_active.is_(True)))).scalar_one_or_none()
            question, namespace, t_from, t_to = inv.question, inv.namespace, inv.time_from, inv.time_to
            self._window = (t_from, t_to)
            env_name = env.name
            max_window = timedelta(minutes=min(env.max_query_window_minutes, self.settings.max_query_window_minutes))
            db.expunge(env)

        for ctype, msg in conn_errors.items():
            self.notes.append(f"{ctype} connector unavailable: {msg}")
        mode = "llm" if llm_cfg else "deterministic"
        if not llm_cfg:
            self.notes.append("No active chat LLM is configured; the report is rule-based.")
        scope_info = {
            "environment": env_name, "namespace": namespace, "from": t_from.isoformat(), "to": t_to.isoformat(),
            "cluster_wide_access": scope.cluster_wide, "connectors": sorted(connectors), "unavailable_connectors": conn_errors,
        }
        await self._emit("scope", {**scope_info, "mode": mode, "llm": f"{llm_cfg.provider}/{llm_cfg.model}" if llm_cfg else None})

        ctx = ToolContext(self.id, env, scope, principal, t_from, t_to, connectors, max_window)

        async with session_scope() as db:
            executor = ToolExecutor(db, ctx, next_ref=self._next_ref, timeout_seconds=min(45, self.settings.max_duration_seconds))

            # 1. Baseline evidence (always, deterministic)
            await self._emit("phase", {"phase": "evidence", "label": "Collecting baseline evidence"})
            await self._baseline(executor, ctx, namespace, question)
            await db.commit()

            # 2. Follow-up evidence: LLM-planned when available, otherwise rule-based
            provider = provider_for(llm_cfg) if llm_cfg else None
            if provider:
                try:
                    await self._llm_rounds(executor, ctx, provider, llm_cfg, question, scope_info)
                except (LLMError, BudgetExceeded) as exc:
                    self.notes.append(f"LLM planning stopped: {getattr(exc, 'message', str(exc))}. Continuing with rule-based follow-ups.")
                    await self._emit("note", {"message": self.notes[-1]})
                    await self._rule_followups(executor, ctx, namespace, question)
            else:
                await self._rule_followups(executor, ctx, namespace, question)
            await db.commit()

        # 3. Correlation
        await self._emit("phase", {"phase": "correlation", "label": "Correlating changes and symptoms"})
        self._correlate()

        # 4. Hypotheses and report
        await self._emit("phase", {"phase": "report", "label": "Writing findings"})
        report: Report | None = None
        if provider:
            try:
                report = await self._llm_report(provider, llm_cfg, question, scope_info)
            except (LLMError, BudgetExceeded, ValueError) as exc:
                self.notes.append(f"LLM report unavailable ({getattr(exc, 'message', str(exc))}); showing the rule-based report.")
        if report is None:
            report = rules_report(question, self.findings, self.evidence, self.notes)
            mode = "deterministic" if not provider else "llm_fallback"
        payload = report.model_dump()
        payload["scope"] = scope_info
        payload["findings"] = self.findings
        await self._finish("completed", payload, None)
        await self._emit("report", {"report": payload})
        return mode

    async def _call(self, executor: ToolExecutor, tool: str, args: dict[str, Any], purpose: str | None = None) -> ToolOutcome | None:
        key = tool + json.dumps(args, sort_keys=True)
        if key in self.calls_seen:
            return None
        if self.tool_calls >= self.settings.max_tool_calls:
            if "tool budget" not in " ".join(self.notes):
                self.notes.append(f"Tool-call budget of {self.settings.max_tool_calls} reached.")
            return None
        self.calls_seen.add(key)
        self.tool_calls += 1
        await self._emit("tool_call", {"tool": tool, "args": args, "purpose": purpose})
        out = await executor.run(tool, args)
        await executor.db.commit()
        record = {"ref": out.evidence_ref, "tool": tool, "args": out.args, "ok": out.ok, "summary": out.summary,
                  "data": out.data, "error": out.error, "error_kind": out.error_kind, "denied": out.denied,
                  "source": out.source, "collected_at": out.collected_at, "truncated": out.truncated}
        if out.evidence_ref:
            self.evidence.append(record)
        else:
            self.rejected.append({"tool": tool, "args": args, "reason": out.error, "kind": out.error_kind})
        await self._emit("tool_result", {k: v for k, v in record.items() if k != "data"} | {"duration_ms": out.duration_ms})
        return out

    async def _baseline(self, executor: ToolExecutor, ctx: ToolContext, namespace: str, question: str) -> None:
        namespaces: list[str]
        if namespace == "*":
            if "kubernetes" not in ctx.connectors:
                namespaces = []
            else:
                try:
                    all_ns = await ctx.connector("kubernetes").list_namespaces()
                except Exception as exc:
                    self.notes.append(f"Could not list namespaces ({getattr(exc, 'message', type(exc).__name__)}).")
                    all_ns = []
                namespaces = ctx.scope.filter(all_ns)[:MAX_NAMESPACES_SWEEP]
                if len(ctx.scope.filter(all_ns)) > MAX_NAMESPACES_SWEEP:
                    self.notes.append(f"Only the first {MAX_NAMESPACES_SWEEP} permitted namespaces were swept; pick a namespace for depth.")
                for ns in namespaces:
                    await self._call(executor, "k8s_list_pods", {"namespace": ns, "unhealthy_only": True}, "sweep for unhealthy pods")
                troubled = [e["args"]["namespace"] for e in self.evidence if e["tool"] == "k8s_list_pods" and e["ok"] and e["data"]]
                namespaces = troubled[:3]
        else:
            namespaces = [namespace]
        if "kubernetes" in ctx.connectors:
            for ns in namespaces:
                out = await self._call(executor, "k8s_triage", {"namespace": ns}, "namespace health check")
                if out and out.ok:
                    for f in out.data.get("findings", []):
                        self.findings.append({**f, "namespace": ns, "evidence": [out.evidence_ref]})
            if ctx.scope.cluster_wide and (namespace == "*" or any(f["kind"] in ("unschedulable", "evicted") for f in self.findings)):
                await self._call(executor, "k8s_nodes", {}, "node health")
        else:
            self.notes.append("No Kubernetes connector: live cluster state is missing.")
        if "prometheus" in ctx.connectors:
            await self._call(executor, "prom_alerts", {"firing_only": True}, "firing alerts")
        trace = TRACE_ID.search(question)
        if trace and "loki" in ctx.connectors and namespace != "*":
            await self._call(executor, "loki_search", {"namespace": namespace, "trace_id": trace.group(0).lower(), "limit": 200}, "logs for the trace ID")

    async def _rule_followups(self, executor: ToolExecutor, ctx: ToolContext, namespace: str, question: str = "") -> None:
        await self._emit("phase", {"phase": "followup", "label": "Following up on findings"})
        for f in self.findings[:6]:
            ns = f.get("namespace") or namespace
            res = f["resources"][0] if f.get("resources") else ""
            kind, _, name = res.partition("/")
            if kind == "pod":
                if f["kind"] == "crashloop":
                    out = await self._call(executor, "k8s_pod_logs", {"namespace": ns, "pod": name, "previous": True, "tail_lines": 120}, "logs of the crashed container")
                    if out and not out.ok:
                        await self._call(executor, "k8s_pod_logs", {"namespace": ns, "pod": name, "tail_lines": 120}, "current container logs")
                    await self._call(executor, "k8s_describe_pod", {"namespace": ns, "pod": name}, "container configuration")
                elif f["kind"] == "oomkilled":
                    await self._call(executor, "k8s_describe_pod", {"namespace": ns, "pod": name}, "memory limits")
                    if "prometheus" in ctx.connectors:
                        await self._call(executor, "prom_pod_resources", {"namespace": ns, "pod_regex": re.escape(name).replace("\\-", "-")}, "memory usage vs limit")
                elif f["kind"] in ("image_pull", "config_error", "unschedulable", "not_ready", "pending"):
                    await self._call(executor, "k8s_describe_pod", {"namespace": ns, "pod": name}, f"details for {f['kind']}")
            elif kind == "deployment" and f["kind"] == "workload_unavailable":
                await self._call(executor, "k8s_rollout_history", {"namespace": ns, "deployment": name}, "recent rollouts")
        if namespace != "*" and "kubernetes" in ctx.connectors and CHANGE_WORDS.search(question):
            await self._change_followups(executor, namespace, question)
        if "loki" in ctx.connectors and namespace != "*":
            await self._call(executor, "loki_search", {"namespace": namespace, "errors_only": True, "limit": 80}, "recent error logs")

    async def _change_followups(self, executor: ToolExecutor, namespace: str, question: str) -> None:
        """Latency / regression questions: look for rollouts in the window and read the logs of the
        services the question names (rule-based mode; an LLM plans this itself)."""
        triage = next((e for e in self.evidence if e["tool"] == "k8s_triage" and e["ok"] and e["args"].get("namespace") == namespace), None)
        deployments = [w["name"] for w in ((triage or {}).get("data") or {}).get("workloads", []) if w.get("kind") == "Deployment"]
        words = set(re.findall(r"[a-z0-9]{3,}", question.lower()))
        mentioned = lambda name: any(part in words for part in name.split("-"))  # noqa: E731
        deployments.sort(key=lambda d: (not mentioned(d), d))
        for d in deployments[:4]:
            await self._call(executor, "k8s_rollout_history", {"namespace": namespace, "deployment": d}, "rollouts inside the window?")
        pods = await self._call(executor, "k8s_list_pods", {"namespace": namespace}, "pods of the services involved")
        if pods and pods.ok:
            named = [p["name"] for p in pods.data if mentioned(p["name"].rsplit("-", 2)[0])][:2]
            for name in named:
                await self._call(executor, "k8s_pod_logs", {"namespace": namespace, "pod": name, "tail_lines": 120}, "application log lines")

    async def _check_budget(self, cfg: LLMConfig) -> None:
        if self.tokens_in + self.tokens_out >= self.settings.max_tokens_per_investigation:
            raise BudgetExceeded(f"token budget of {self.settings.max_tokens_per_investigation} for this investigation used")
        async with session_scope() as db:
            since = datetime.now(timezone.utc) - timedelta(days=1)
            spent = (await db.execute(select(func.coalesce(func.sum(LLMUsage.cost_usd), 0))
                                      .where(LLMUsage.llm_config_id == cfg.id, LLMUsage.created_at >= since))).scalar_one()
        if Decimal(spent) >= Decimal(cfg.daily_budget_usd) and Decimal(cfg.daily_budget_usd) > 0:
            raise BudgetExceeded(f"daily LLM budget of ${cfg.daily_budget_usd} for {cfg.name} reached")

    async def _llm(self, provider, cfg: LLMConfig, system: str, user: str, max_tokens: int) -> dict[str, Any]:
        await self._check_budget(cfg)
        messages = [{"role": "user", "content": user}]
        last_error: Exception | None = None
        for attempt in range(2):
            resp = await complete_metered(provider, system, messages, json_mode=True, max_tokens=max_tokens)
            self.tokens_in += resp.tokens_in
            self.tokens_out += resp.tokens_out
            self.cost += resp.cost_usd
            async with session_scope() as db:
                db.add(LLMUsage(llm_config_id=cfg.id, investigation_id=self.id, provider=cfg.provider, model=cfg.model,
                                tokens_in=resp.tokens_in, tokens_out=resp.tokens_out, cost_usd=resp.cost_usd))
            await self._emit("llm_usage", {"tokens_in": self.tokens_in, "tokens_out": self.tokens_out, "cost_usd": str(self.cost)})
            try:
                return parse_json_object(resp.text)
            except (ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                messages = messages + [{"role": "assistant", "content": resp.text[:4000]},
                                       {"role": "user", "content": "That was not a single valid JSON object. Reply again with only the JSON object."}]
        raise LLMError("bad_response", f"model did not return valid JSON ({last_error})")

    async def _llm_rounds(self, executor: ToolExecutor, ctx: ToolContext, provider, cfg: LLMConfig, question: str, scope_info: dict) -> None:
        tools = [t.schema_for_llm() for t in tools_for_planner(ctx)]
        allowed = {t["name"] for t in tools}
        system = prompts.PLANNER_SYSTEM.replace("{max_calls}", str(MAX_CALLS_PER_ROUND))
        for round_no in range(1, self.settings.max_llm_rounds + 1):
            remaining = self.settings.max_tool_calls - self.tool_calls
            if remaining <= 0:
                break
            await self._emit("phase", {"phase": "planning", "label": f"Planning follow-up evidence (round {round_no})"})
            plan = await self._llm(provider, cfg, system,
                                   prompts.planner_user(question, scope_info, tools, self.evidence, self.findings, remaining, self.rejected),
                                   max_tokens=min(cfg.max_output_tokens, 1200))
            calls = plan.get("tool_calls") if isinstance(plan.get("tool_calls"), list) else []
            if not calls:
                break
            new = 0
            for call in calls[:MAX_CALLS_PER_ROUND]:
                if not isinstance(call, dict):
                    continue
                tool, args = str(call.get("tool", ""))[:80], call.get("args") if isinstance(call.get("args"), dict) else {}
                purpose = str(call.get("purpose", ""))[:120] or None
                if tool not in allowed:
                    # Still goes through the executor so the denial is audited and counted.
                    await self._call(executor, tool, args, purpose)
                    continue
                out = await self._call(executor, tool, args, purpose)
                if out is not None:
                    new += 1
            if plan.get("done") is True or new == 0:
                break

    async def _llm_report(self, provider, cfg: LLMConfig, question: str, scope_info: dict) -> Report:
        raw = await self._llm(provider, cfg, prompts.REPORT_SYSTEM,
                              prompts.report_user(question, scope_info, self.evidence, self.findings, self.notes),
                              max_tokens=max(cfg.max_output_tokens, 1500))
        refs = {e["ref"] for e in self.evidence if e["ref"]}
        return validate_llm_report(raw, refs, self.notes)

    def _correlate(self) -> None:
        """Time correlation between rollouts and problem onset. Correlation, not causation."""
        rollouts = [e for e in self.evidence if e["tool"] == "k8s_rollout_history" and e["ok"]]
        for e in rollouts:
            revs = (e["data"] or {}).get("revisions") or []
            if not revs or not revs[0].get("created"):
                continue
            created = revs[0]["created"]
            dep = e["args"].get("deployment")
            try:
                created_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except ValueError:
                continue
            if self._window and created_at < self._window[0]:
                continue  # rolled out before the window: not a candidate for this symptom
            prev = revs[1] if len(revs) > 1 else None
            changed = prev and prev.get("images") != revs[0].get("images")
            self.findings.append({
                "kind": "recent_rollout", "severity": "medium",
                "title": f"Deployment {dep} revision {revs[0]['revision']} rolled out at {created}",
                "detail": ("Image changed from " + ", ".join(prev["images"]) + " to " + ", ".join(revs[0]["images"]) + ". ") if changed else
                          "Pod template changed (images unchanged). " + "Timing correlation only; verify before attributing cause.",
                "resources": [f"deployment/{dep}"], "suspected_cause": None,
                "next_checks": [f"Compare revision {revs[0]['revision']} with {prev['revision']} configuration" if prev else "Review the latest change"],
                "remediation_hint": f"If the new revision is at fault, roll back deployment/{dep} to revision {prev['revision']} after review." if prev else None,
                "evidence": [e["ref"]], "namespace": e["args"].get("namespace"),
            })
