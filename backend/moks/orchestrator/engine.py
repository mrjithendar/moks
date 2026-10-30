"""The controlled investigation loop.

question -> scope -> baseline evidence -> (LLM-planned or rule-based) follow-up evidence ->
correlation -> hypotheses -> verification -> report. An investigation only proposes remediation;
changes go through action plans (moks/actions).

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
from ..models import SkillPolicy, Environment, Investigation, InvestigationEvent, LLMConfig, LLMUsage, RoleBinding, User
from ..observability import INVESTIGATION_DURATION, INVESTIGATIONS, INVESTIGATIONS_RUNNING
from ..rbac import Binding, P, Principal
from ..scope import ScopeError, effective_scope
from ..tools.contracts import ToolContext
from ..tools.executor import ToolExecutor, ToolOutcome, tools_for_planner
from . import prompts
from .. import conversations
from ..diagnosis.logs import last_failure
from .report import Report, parse_json_object, rules_report, validate_llm_report, verify_causes

log = logging.getLogger("moks.orchestrator")

TRACE_ID = re.compile(r"\b[0-9a-f]{32}\b", re.I)
CHANGE_WORDS = re.compile(r"(?i)\b(latenc\w*|slow\w*|deploy\w*|rollout|rolled|release\w*|change\w*|regress\w*|timeout\w*|degrad\w*)\b")
MAX_CALLS_PER_ROUND = 4
MAX_NAMESPACES_SWEEP = 10


MAX_LOG_PODS = 3
LOG_TAIL = 150
LOG_FINDINGS = {"crashloop", "oomkilled", "oomkilled_earlier", "not_ready", "config_error", "evicted"}


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
    def __init__(self, investigation_id: uuid.UUID, worker_id: str, connectors_override: dict | None = None, use_llm: bool = True):
        self.connectors_override = connectors_override  # evaluation runs supply fixture connectors
        self.use_llm = use_llm
        self.id = investigation_id
        self.worker_id = worker_id
        self.settings = get_settings()
        self.seq = 0
        self.ref = 0
        self.evidence: list[dict[str, Any]] = []
        self.history: list[dict[str, str]] = []
        self.findings: list[dict[str, Any]] = []
        self.notes: list[str] = []
        self.tool_calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost = Decimal("0")
        self.calls_seen: set[str] = set()
        self.rejected: list[dict[str, Any]] = []
        self._window: tuple[datetime, datetime] | None = None
        self.skills: list = []  # (Skill, score, reasons) selected for this investigation
        self.focus_namespace: str | None = None

    async def _emit(self, type_: str, payload: dict[str, Any]) -> None:
        self.seq += 1
        await emit(self.id, self.seq, type_, payload)
        if type_ == "phase":
            async with session_scope() as db:
                await db.execute(update(Investigation).where(Investigation.id == self.id).values(stage=payload["phase"]))

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
            await conversations.record_reply(db, inv, (report or {}).get("summary") or error or "No report was produced.")
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
            if self.connectors_override is not None:
                connectors, conn_errors = self.connectors_override, {}
            else:
                connectors, conn_errors = await registry.for_environment(db, env.id)
            llm_cfg = (await db.execute(select(LLMConfig).where(LLMConfig.purpose == "chat", LLMConfig.is_active.is_(True)))).scalar_one_or_none() if self.use_llm else None
            question, namespace, t_from, t_to = inv.question, inv.namespace, inv.time_from, inv.time_to
            if inv.conversation_id:
                self.history = await conversations.history(db, inv.conversation_id, inv.id)
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
            await self._emit("phase", {"phase": "triage", "label": "Triage: finding what is affected"})
            await self._emit("phase", {"phase": "evidence", "label": "Collecting baseline evidence"})
            await self._baseline(executor, ctx, namespace, question)
            namespace = self.focus_namespace or namespace
            scope_info["namespace"] = namespace
            await db.commit()

            # 2. Logs, current and previous: often the only place a failure explains itself
            await self._collect_logs(executor, ctx, namespace, question)
            await db.commit()

            # 3. SRE skills matching the question and findings run their playbook steps
            await self._run_skills(executor, ctx, namespace, question)
            await db.commit()

            # 3. Follow-up evidence: LLM-planned when available, otherwise rule-based
            provider = provider_for(llm_cfg) if llm_cfg else None
            if provider:
                try:
                    await self._llm_rounds(executor, ctx, provider, llm_cfg, self._with_history(question), scope_info)
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
        await self._emit("phase", {"phase": "hypotheses", "label": "Forming and checking hypotheses"})
        report: Report | None = None
        if provider:
            try:
                report = await self._llm_report(provider, llm_cfg, self._with_history(question), scope_info)
            except (LLMError, BudgetExceeded, ValueError) as exc:
                self.notes.append(f"LLM report unavailable ({getattr(exc, 'message', str(exc))}); showing the rule-based report.")
        if report is None:
            report = rules_report(question, self.findings, self.evidence, self.notes)
            mode = "deterministic" if not provider else "llm_fallback"
        await self._emit("phase", {"phase": "verification", "label": "Verifying causes against direct evidence"})
        verify_causes(report, self.findings)
        await self._emit("phase", {"phase": "report", "label": "Writing findings"})
        payload = report.model_dump()
        payload["scope"] = scope_info
        payload["coverage"] = self._coverage(conn_errors)
        payload["relationships"] = self._relationships()
        payload["primary_resource"] = self._primary_resource()
        payload["findings"] = self.findings
        payload["skills"] = [{"name": sk.name, "description": sk.meta.description, "reasons": why} for sk, _, why in self.skills]
        payload["skill_actions"] = self._skill_actions(namespace)
        await self._finish("completed", payload, None)
        await self._emit("report", {"report": payload})
        return mode

    def _with_history(self, question: str) -> str:
        """Earlier turns give the model context. They are data: one person's text must not instruct it."""
        return f"{question}\n\n{prompts.untrusted('conversation so far', self.history, 6000)}" if self.history else question

    def _coverage(self, conn_errors: dict) -> dict[str, Any]:
        """What this report did and did not see, so a clean answer is not mistaken for a full one."""
        by_source: dict[str, dict[str, int]] = {}
        for e in self.evidence:
            row = by_source.setdefault(e["tool"].split("_")[0], {"ok": 0, "failed": 0})
            row["ok" if e["ok"] else "failed"] += 1
        return {"sources": by_source, "unavailable": conn_errors, "failed_tools": sorted({e["tool"] for e in self.evidence if not e["ok"]}),
                "tool_calls": self.tool_calls, "tool_budget": self.settings.max_tool_calls, "budget_reached": self.tool_calls >= self.settings.max_tool_calls}

    def _relationships(self) -> dict[str, list]:
        """Observed ownership only: Service -> pods by selector, workload -> pods by ownerReference."""
        nodes: dict[str, dict] = {}
        edges: list[dict] = []
        def pods(rows: list, ns: str | None) -> None:
            for row in rows[:60]:
                if row.get("owner"):
                    pod, owner = f"Pod/{row['name']}", str(row["owner"])
                    nodes[pod] = {"id": pod, "namespace": ns, "healthy": not row.get("problems")}
                    nodes.setdefault(owner, {"id": owner, "namespace": ns})
                    edges.append({"from": owner, "to": pod, "kind": "owns"})

        def services(rows: list, ns: str | None) -> None:
            for row in rows[:60]:
                svc = f"Service/{row.get('name')}"
                nodes[svc] = {"id": svc, "namespace": ns, "healthy": bool((row.get("endpoints") or {}).get("ready")) or not row.get("selector")}
                for pod in (row.get("matchingPods") or [])[:10]:
                    edges.append({"from": svc, "to": f"Pod/{pod}", "kind": "selects"})

        for e in self.evidence:
            if not e["ok"]:
                continue
            ns, data = e["args"].get("namespace"), e.get("data")
            if e["tool"] == "k8s_list_pods" and isinstance(data, list):
                pods(data, ns)
            elif e["tool"] == "k8s_services" and isinstance(data, list):
                services(data, ns)
            elif e["tool"] == "k8s_triage" and isinstance(data, dict):
                pods(data.get("unhealthyPods") or [], ns)
                services(data.get("services") or [], ns)
        return {"nodes": list(nodes.values())[:80], "edges": edges[:120]}

    def _primary_resource(self) -> str | None:
        ranked = sorted(self.findings, key=lambda f: {"high": 0, "medium": 1, "low": 2}.get(f.get("severity"), 3))
        return next((f["resources"][0] for f in ranked if f.get("resources")), None)

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

    async def _focus(self, ctx: ToolContext, question: str) -> tuple[str | None, str]:
        """For all-namespace investigations: find the namespace the question is about by matching
        its words against namespace names, then Deployment/StatefulSet names (one list call)."""
        if "kubernetes" not in ctx.connectors:
            return None, "no kubernetes connector"
        words = set(re.findall(r"[a-z0-9][a-z0-9-]{2,62}", question.lower()))
        k = ctx.connector("kubernetes")
        try:
            nss = ctx.scope.filter(await k.list_namespaces())
        except Exception:
            return None, "could not list namespaces"
        direct = [n for n in nss if n in words]
        if len(direct) == 1:
            return direct[0], f"namespace {direct[0]} is named in the question"
        hits: dict[str, list[str]] = {}
        for ns in nss[:40]:
            try:
                for w in await k.workloads(ns):
                    base = w["name"]
                    parts = set(base.split("-")) | {base}
                    if words & parts - {"service", "server", "api", "app", "web", "db", "the", "and"}:
                        hits.setdefault(ns, []).append(base)
            except Exception:
                continue
        if len(hits) == 1:
            ns, names = next(iter(hits.items()))
            return ns, f"workload {', '.join(sorted(set(names))[:3])} named in the question lives in {ns}"
        if hits:
            return None, "the question matches workloads in several namespaces (" + ", ".join(sorted(hits)) + "); sweeping all"
        return None, "no namespace or workload named; sweeping all permitted namespaces"

    async def _baseline(self, executor: ToolExecutor, ctx: ToolContext, namespace: str, question: str) -> None:
        namespaces: list[str]
        if namespace == "*":
            focus, why = await self._focus(ctx, question)
            self.notes.append(f"Scope: {why}.")
            await self._emit("note", {"message": f"Scope: {why}"})
            if focus:
                async with session_scope() as db:
                    await db.execute(update(Investigation).where(Investigation.id == self.id).values(namespace=focus))
                self.focus_namespace = focus
                namespace = focus
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

    async def _log_targets(self, executor: ToolExecutor, namespace: str, question: str) -> list[tuple[str, str]]:
        """Pods worth reading: those with a finding, then any the question names."""
        targets: list[tuple[str, str]] = []
        for f in self.findings:
            res = next((r for r in f.get("resources", []) if r.startswith("pod/")), None)
            if res and f["kind"] in LOG_FINDINGS and (f.get("namespace"), res[4:]) not in targets:
                targets.append((f.get("namespace"), res[4:]))
        if len(targets) < MAX_LOG_PODS and namespace != "*":
            words = set(re.findall(r"[a-z0-9]{3,}", question.lower()))
            pods = await self._call(executor, "k8s_list_pods", {"namespace": namespace}, "pods the question may name")
            if pods and pods.ok:
                targets += [(namespace, p["name"]) for p in pods.data if words & set(p["name"].split("-")) and (namespace, p["name"]) not in targets]
        return targets[:MAX_LOG_PODS]

    async def _collect_logs(self, executor: ToolExecutor, ctx: ToolContext, namespace: str, question: str) -> None:
        """Runs in every mode, so finding a cause never depends on the planner thinking to look. Reads
        each container's previous instance if it restarted, its current logs, and Loki when a previous
        instance has been rotated away."""
        if "kubernetes" not in ctx.connectors:
            return
        await self._emit("phase", {"phase": "logs", "label": "Reading current and previous logs"})
        for ns, pod in await self._log_targets(executor, namespace, question):
            out = await self._call(executor, "k8s_describe_pod", {"namespace": ns, "pod": pod}, "containers, restarts and exit codes")
            if not out or not out.ok:
                continue
            containers = out.data.get("containers") or []
            many = len(containers) > 1
            for c in containers:
                name = c.get("name")
                last = (c.get("lastState") or {}).get("terminated") or {}
                restarted = bool(last) or (c.get("restartCount") or 0) > 0
                args = {"namespace": ns, "pod": pod, "tail_lines": LOG_TAIL, **({"container": name} if many else {})}
                previous = None
                if restarted:
                    previous = await self._call(executor, "k8s_pod_logs", {**args, "previous": True}, f"what {name} wrote before it stopped")
                    if previous and previous.ok and last.get("exitCode") not in (None, 0):
                        self._crash_reason(ns, pod, name, last, previous)
                    elif previous and not previous.ok:
                        self.notes.append(f"Previous logs of {pod}/{name} are not available ({previous.error_kind}).")
                await self._call(executor, "k8s_pod_logs", args, f"current logs of {name}")
                if restarted and (previous is None or not previous.ok) and "loki" in ctx.connectors:
                    await self._call(executor, "loki_logs", {"namespace": ns, "pod": pod, "level": "error", "limit": 100}, f"earlier errors of {name} in Loki")

    def _crash_reason(self, ns: str, pod: str, container: str, last: dict, out: ToolOutcome) -> None:
        line = last_failure(out.data.get("lines") or [])
        if not line:
            return
        self.findings.append({"kind": "crash_log_reason", "severity": "high", "namespace": ns, "resources": [f"pod/{pod}"], "evidence": [out.evidence_ref],
                              "title": f"{pod}/{container} exited with code {last.get('exitCode')} after logging: {line[:240]}",
                              "detail": f"Last failure in the previous container's log (reason {last.get('reason') or 'unknown'}).", "next_checks": []})

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

    async def _run_skills(self, executor: ToolExecutor, ctx: ToolContext, namespace: str, question: str) -> None:
        from .. import skills as skill_lib
        from ..tools.builtin import REGISTRY

        kinds = {f["kind"] for f in self.findings}
        async with session_scope() as db:
            off = set((await db.execute(select(SkillPolicy.name).where(SkillPolicy.enabled.is_(False)))).scalars())
        chosen = []
        for entry in skill_lib.select(question, kinds):
            sk = entry[0]
            state = skill_lib.availability(sk, set(ctx.connectors), None)
            if sk.name in off:
                continue
            if not state["available"]:
                self.notes.append(f"Skill {sk.name} skipped: {'; '.join(state['reasons'])}.")
                continue
            chosen.append(entry)
        self.skills = chosen
        if not self.skills:
            return
        await self._emit("skills", {"skills": [{"name": sk.name, "reasons": why} for sk, _, why in self.skills]})
        trace = TRACE_ID.search(question)
        base = {"namespace": "" if namespace == "*" else namespace, "question": question[:200],
                "trace_id": trace.group(0).lower() if trace else ""}
        budget = max(0, self.settings.max_tool_calls - self.tool_calls - 4)  # leave room for follow-ups
        for sk, _, _ in self.skills:
            await self._emit("phase", {"phase": "skill", "label": f"Running skill {sk.name}"})
            for step in sk.meta.steps:
                spec = REGISTRY[step.tool]
                if not all(ctx.available(c) for c in (spec.connector_type, *spec.also_requires)):
                    continue  # connector not configured here: the skill degrades gracefully
                if spec.cluster_scoped and not ctx.scope.cluster_wide:
                    continue
                if step.when_question and not re.search(step.when_question, question, re.I):
                    continue
                for c in skill_lib.contexts_for(step, self.findings, base):
                    args = skill_lib.render(step.args, c)
                    if args is None or budget <= 0:
                        continue  # a needed variable (e.g. deployment of a StatefulSet pod) is unknown
                    out = await self._call(executor, step.tool, args, f"[{sk.name}] {step.purpose or step.tool}")
                    if out is not None:
                        budget -= 1

    def _skill_actions(self, namespace: str) -> list[dict]:
        """Deterministic, allowlisted remediation suggestions from skills whose `when` matched a finding.
        Suggestions only: turning one into an action plan still needs review (and approval in production)."""
        from .. import skills as skill_lib

        out, seen = [], set()
        for sk, _, _ in self.skills:
            for r in sk.meta.remediation:
                for f in [f for f in self.findings if f["kind"] == r.when][:3]:
                    ctx = skill_lib.contexts_for(skill_lib.StepSpec(tool="k8s_events", for_each=f"finding.{r.when}"), [f], {"namespace": namespace})
                    c = ctx[0] if ctx else {"namespace": namespace}
                    target = skill_lib.render(r.target, c) if r.target else ""
                    if r.target and target is None:
                        continue
                    if not r.target and f.get("resources"):
                        res = f["resources"][0]
                        rk, _, rn = res.partition("/")
                        target = {"deployment": f"Deployment/{rn}", "service": f"Service/{rn}"}.get(rk, "")
                    kind, _, name = (target or "").partition("/")
                    params = dict(r.params) or (f.get("suggested") or {}).get(r.action, {})
                    if r.action == "patch_resources" and (kind != "Deployment" or not params):
                        continue  # only Deployments with concrete, measured values
                    key = (r.action, target, str(params))
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append({"skill": sk.name, "action": r.action, "target_kind": kind or None, "target_name": name or None,
                                "namespace": c.get("namespace") or f.get("namespace") or namespace, "params": params, "note": r.note,
                                "finding": f["title"], "evidence": f.get("evidence", [])})
        return out

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
        system = prompts.PLANNER_SYSTEM.replace("{max_calls}", str(MAX_CALLS_PER_ROUND)) + prompts.skills_section(self.skills)
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
        raw = await self._llm(provider, cfg, prompts.REPORT_SYSTEM + prompts.skills_section(self.skills),
                              prompts.report_user(question, scope_info, self.evidence, self.findings, self.notes),
                              max_tokens=max(cfg.max_output_tokens, 1500))
        refs = {e["ref"] for e in self.evidence if e["ref"]}
        return validate_llm_report(raw, refs, self.notes)

    def _correlate(self) -> None:
        """Time correlation between rollouts and problem onset (correlation, not causation), and
        capacity risks from rightsizing evidence."""
        from ..diagnosis.config_audit import findings_from

        for e in [e for e in self.evidence if e["tool"] == "k8s_config_audit" and e["ok"]]:
            known = {(f["kind"], tuple(f.get("resources", []))) for f in self.findings}
            for f in findings_from(e["data"] or {}, e["args"].get("namespace", ""), e["ref"]):
                if (f["kind"], tuple(f["resources"])) not in known:
                    self.findings.append(f)
        for e in [e for e in self.evidence if e["tool"] == "finops_rightsizing" and e["ok"]]:
            for r in ((e["data"] or {}).get("recommendations") or [])[:12]:
                rec = r["recommended"]
                params = {"container": r["container"], "requests": {"cpu": rec["cpu_request"], "memory": rec["mem_request"]},
                          "limits": {"memory": rec["mem_limit"]} if rec.get("mem_limit") else {}}
                suggested = {"patch_resources": params} if r["workload"].startswith("Deployment/") else {}
                if r["action"] == "reduce":
                    self.findings.append({
                        "kind": "over_provisioned", "severity": "low",
                        "title": f"{r['workload']} container {r['container']} requests more than it uses (save ~{r['monthly_saving']}/month)",
                        "detail": f"Observed p95 CPU {r['observed']['cpu_p95']} cores, max memory {r['observed']['mem_max']}; current {r['current']}; recommended {rec}.",
                        "resources": [r["workload"].lower()], "suspected_cause": None, "next_checks": [], "suggested": suggested,
                        "remediation_hint": f"Reduce {r['container']} requests to the recommendation via a reviewed patch_resources plan.",
                        "evidence": [e["ref"]], "namespace": r["namespace"]})
                    continue
                if r["action"] != "increase":
                    continue
                oom = any("OOM" in f for f in r["findings"])
                self.findings.append({
                    "suggested": suggested,
                    "kind": "memory_near_limit" if oom else "under_provisioned", "severity": "medium",
                    "title": f"{r['workload']} container {r['container']} " + ("is within 10% of its memory limit" if oom else "uses more CPU than it requests"),
                    "detail": f"Observed max memory {r['observed']['mem_max']}, p95 CPU {r['observed']['cpu_p95']} cores; current {r['current']}; "
                              f"recommended {r['recommended']}. " + "; ".join(r["findings"]),
                    "resources": [r["workload"].lower()], "suspected_cause": "Requests/limits sized below observed usage." if not oom else
                    "Memory limit too close to the working set; a load spike or leak will OOMKill it.",
                    "next_checks": ["Check whether usage grows over days (leak) or tracks load (prom_pod_resources)"],
                    "remediation_hint": f"Raise {r['container']} resources to the recommendation via a reviewed patch_resources plan.",
                    "evidence": [e["ref"]], "namespace": r["namespace"],
                })
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
