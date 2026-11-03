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
import hashlib
import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from .. import audit
from ..config import get_settings
from ..connectors import registry
from ..db import session_scope
from ..auth.revocation import CHECK_SECONDS as WATCH_SECONDS, AccessGuard, AccessRevoked
from ..llm.lc import MoksChatModel
from ..llm.providers import LLMError, provider_for
from ..models import Environment, Evidence, Investigation, InvestigationCheckpoint, InvestigationEvent, LLMConfig, LLMUsage, RoleBinding, SkillVersion, User
from opentelemetry import trace as trace_api

from ..observability import INVESTIGATION_DURATION, INVESTIGATIONS, INVESTIGATIONS_RUNNING, continue_trace, tracer
from ..rbac import Binding, P, Principal
from ..redaction import redact_obj
from ..scope import ScopeError, effective_scope
from ..tools.contracts import ToolContext
from ..tools.executor import ToolExecutor, ToolOutcome, tools_for_planner
from ..tools.lc import as_tools
from . import graph, prompts, relevance
from .. import conversations
from ..diagnosis.logs import last_failure
from ..llm.budget import CHARS_PER_TOKEN
from .evidence import HISTORICAL, LIVE, Ledger, classify
from .intent import CONCEPT_TOOLS, FILTERED_CLUSTER_TOOLS, GUIDANCE_TOOLS, LLM_SYSTEM, RequestContext, build as build_request, classify as classify_request, names_in as intent_names, refine
from .report import Report, focus, parse_json_object, rules_report, validate_claims, validate_llm_report, verify_causes

log = logging.getLogger("moks.orchestrator")

TRACE_ID = re.compile(r"\b[0-9a-f]{32}\b", re.I)
DNS_FAILURE = re.compile(r"(?i)no such host|nxdomain|could not resolve|temporary failure in name resolution|name or service not known|lookup [\w.-]+ on [\d.]+:53|server misbehaving")
CHANGE_WORDS = re.compile(r"(?i)\b(latenc\w*|slow\w*|deploy\w*|rollout|rolled|release\w*|change\w*|regress\w*|timeout\w*|degrad\w*)\b")
MAX_CALLS_PER_ROUND = 4
MAX_NAMESPACES_SWEEP = 10


MAX_LOG_PODS = 3
LOG_TAIL = 150
PLAN_TOKENS = 1200
REPORT_TOKENS = 1500   # asked for, within the configured cap; a smaller cap is honored
MIN_PLAN_TOKENS = 150
MIN_REPORT_TOKENS = 300  # below this a report would be cut off mid-JSON, so the rule-based one is used
LOG_FINDINGS = {"crashloop", "oomkilled", "oomkilled_earlier", "not_ready", "config_error", "evicted"}


CHECKPOINT_VERSION = 1
MAX_OWNER_LOOKUPS = 10
RECOLLECT = {"k8s_triage", "k8s_list_pods", "prom_alerts"}  # what a resumed run reads again when its evidence has aged
MAX_RECOLLECT = 4


class BudgetExceeded(Exception):
    pass


class OwnershipLost(Exception):
    """This worker's claim is gone. Whatever it holds must not be written anywhere."""


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


async def emit_next(investigation_id: uuid.UUID, type_: str, payload: dict[str, Any]) -> None:
    """An event from outside the run, with the next free sequence number."""
    async with session_scope() as db:
        seq = (await db.execute(select(func.coalesce(func.max(InvestigationEvent.seq), 0)).where(InvestigationEvent.investigation_id == investigation_id))).scalar_one()
        db.add(InvestigationEvent(investigation_id=investigation_id, seq=seq + 1, type=type_, payload=payload))


def _call_key(tool: str, args: dict[str, Any]) -> str:
    return tool + json.dumps(args, sort_keys=True)


class Investigator:
    def __init__(self, investigation_id: uuid.UUID, worker_id: str, connectors_override: dict | None = None, use_llm: bool = True,
                 token: uuid.UUID | None = None, attempt: int = 1):
        self.token = token
        self.attempt = attempt
        self.lost = False
        self.completed_nodes: list[str] = []
        self.resumed_from: str | None = None
        self.connector_ids: dict[str, str | None] = {}
        self.llm_key: str | None = None
        self.guard: AccessGuard | None = None
        self.owner: uuid.UUID | None = None
        self.revoked: str | None = None
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
        self.skills: list = []  # (Skill, score, reasons) selected for this investigation; these copies are the pin
        self.skill_runs: list[dict] = []  # what each did: steps run and skipped, evidence, checks
        self.skill_rejected: list[dict] = []
        self.skill_generation: int | None = None
        self.requested_skill: str | None = None
        self.focus_namespace: str | None = None
        self.versions: dict[str, Any] = {}
        # The validated request (orchestrator/intent.py) and what was decided about it, for the report and traces.
        self.request: RequestContext | None = None
        self.sources: list[dict[str, Any]] = []     # documents and files read for guidance, with relevance decisions
        self.gaps: list[dict[str, Any]] = []
        self.retrieval_notes: list[str] = []
        self.shared_findings: list[dict[str, Any]] = []  # shared infrastructure, kept apart from the target's findings
        self.excluded: list[dict[str, Any]] = []         # evidence about other targets, left out of the answer with its provenance
        self.trace: dict[str, Any] = {"tools": [], "sources": [], "scope_rejections": []}
        self.environment_info: dict[str, Any] = {}
        self.conversation_namespace: str | None = None
        self.llm_failures: tuple = (Exception,)

    @property
    def tool_budget(self) -> int:
        """Tool calls this request may use: the intent's budget, never above the global ceiling."""
        cap = self.settings.max_tool_calls
        if self.request is None:
            return cap
        own = (self.settings.intent_tool_budgets or {}).get(self.request.primary) or cap
        return max(1, min(cap, own))

    async def _fence(self, db) -> None:
        """Inside a transaction that is about to write: the row lock holds off the reaper until the
        commit, so a write either lands while the claim is good or not at all."""
        if self.token is None:
            return
        held = (await db.execute(text("SELECT 1 FROM investigations WHERE id = :i AND execution_token = :t FOR SHARE"), {"i": self.id, "t": self.token})).first()
        if held is None:
            self.lost = True
            raise OwnershipLost(str(self.id))

    async def _emit(self, type_: str, payload: dict[str, Any]) -> None:
        self.seq += 1
        async with session_scope() as db:
            await self._fence(db)
            db.add(InvestigationEvent(investigation_id=self.id, seq=self.seq, type=type_, payload=payload))
            if type_ == "phase":
                await db.execute(update(Investigation).where(Investigation.id == self.id).values(stage=payload["phase"]))

    def _next_ref(self) -> str:
        self.ref += 1
        return f"E{self.ref}"

    # ---- entry point ----------------------------------------------------------------------

    async def _adopt(self) -> None:
        """Runs started directly, not through the worker's claim, still run under a token."""
        async with session_scope() as db:
            row = (await db.execute(text("""UPDATE investigations SET execution_token = gen_random_uuid(), status = 'running', worker_id = :w, started_at = COALESCE(started_at, now()),
                                            heartbeat_at = now(), lease_expires_at = now() + make_interval(secs => :l), attempts = attempts + 1
                                            WHERE id = :i AND status IN ('queued', 'running') RETURNING execution_token, attempts"""),
                                    {"i": self.id, "w": self.worker_id[:100], "l": self.settings.investigation_lease_seconds})).first()
        if row:
            self.token, self.attempt = row

    async def run(self) -> None:
        if self.token is None:
            await self._adopt()
        async with session_scope() as db:
            queued = (await db.execute(select(InvestigationEvent.payload).where(InvestigationEvent.investigation_id == self.id, InvestigationEvent.seq == 0))).scalar_one_or_none()
        with tracer().start_as_current_span("investigation", context=continue_trace((queued or {}).get("traceparent"))) as span:
            span.set_attribute("moks.investigation.id", str(self.id))
            await self._run()

    async def _run(self) -> None:
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
        except OwnershipLost:
            status = "abandoned"
        except asyncio.CancelledError:
            status, error = ("abandoned", None) if self.lost else ("cancelled", self.revoked or "cancelled by user")
            uncancel = getattr(task, "uncancel", None)
            if uncancel:
                uncancel()
        except TimeoutError:
            status, error = "failed", f"time limit of {self.settings.max_duration_seconds}s reached"
        except ScopeError as exc:
            status, error = "failed", f"not permitted: {exc}"
            if isinstance(exc, AccessRevoked):
                self.revoked = error
        except Exception as exc:
            log.exception("investigation failed", extra={"event": "investigation_failed", "investigation_id": str(self.id)})
            status, error = "failed", f"internal error ({type(exc).__name__})"
        finally:
            watcher.cancel()
            INVESTIGATIONS_RUNNING.dec()
        try:
            if status != "completed" and status != "abandoned":
                # Keep the partial evidence useful: write a rule-based report of what was collected.
                partial = rules_report("", self.findings, self.evidence, self.notes + [f"Investigation {status}: {error}"])
                if not await self._finish(status, partial.model_dump() if self.evidence and not self.revoked else None, error):
                    status = "abandoned"
                else:
                    await self._emit("error" if status == "failed" else "cancelled", {"message": error})
            if status != "abandoned":
                await self._emit("done", {"status": status})
        except OwnershipLost:
            status = "abandoned"
        except Exception:  # the database went away: the lease lapses and the reaper decides what happens next
            log.exception("could not record the outcome", extra={"event": "investigation_outcome_lost", "investigation_id": str(self.id)})
            status = "abandoned"
        if status == "abandoned":
            log.warning("stopped: this worker no longer owns the investigation", extra={"event": "investigation_abandoned", "investigation_id": str(self.id)})
        INVESTIGATIONS.labels(status, mode).inc()
        INVESTIGATION_DURATION.labels(mode).observe(time.monotonic() - start)
        log.info("investigation finished", extra={"event": "investigation_finished", "investigation_id": str(self.id), "operation": mode, "outcome": "success" if status == "completed" else "failure",
                                                  "durationMs": int((time.monotonic() - start) * 1000), **({} if status == "completed" else {"errorCode": status})})

    async def _renew(self) -> bool:
        """False when the work should stop for a reason recorded on the object. Raises if the database cannot say."""
        async with session_scope() as db:
            if self.token is None:
                inv = await db.get(Investigation, self.id)
                if inv is None or inv.cancel_requested:
                    return False
                inv.heartbeat_at = datetime.now(timezone.utc)
                return True
            row = (await db.execute(text("""UPDATE investigations SET heartbeat_at = now(), lease_expires_at = now() + make_interval(secs => :l)
                                            WHERE id = :i AND execution_token = :t AND status = 'running' AND lease_expires_at > now() RETURNING cancel_requested"""),
                                    {"i": self.id, "t": self.token, "l": self.settings.investigation_lease_seconds})).first()
        if row is None:
            self.lost = True
            return False
        return not row[0]

    async def _watch(self, task: asyncio.Task) -> None:
        last_check = renewed = time.monotonic()
        while True:
            await asyncio.sleep(1.0 if WATCH_SECONDS >= 1 else WATCH_SECONDS)
            try:
                if not await self._renew():
                    task.cancel()
                    return
                renewed = time.monotonic()
                if self.guard is not None and time.monotonic() - last_check >= WATCH_SECONDS:
                    last_check = time.monotonic()
                    await self.guard.check()
            except AccessRevoked as exc:
                self.revoked = f"stopped because access was revoked: {exc}"
                log.warning("investigation stopped: access revoked", extra={"event": "investigation_revoked", "investigation_id": str(self.id)})
                task.cancel()
                return
            except Exception:  # a short outage must not kill the run, but past the lease it may no longer be ours
                log.warning("heartbeat failed", extra={"event": "heartbeat_failed", "investigation_id": str(self.id)})
                if self.token is not None and time.monotonic() - renewed > self.settings.investigation_lease_seconds:
                    self.lost = True
                    task.cancel()
                    return

    async def _finish(self, status: str, report: dict | None, error: str | None) -> bool:
        """The one transition out of running. Exactly one caller wins it, so there is one reply and one
        audit record however many workers thought they owned the run."""
        async with session_scope() as db:
            q = update(Investigation).where(Investigation.id == self.id)
            if self.token is not None:
                q = q.where(Investigation.execution_token == self.token, Investigation.status == "running")
            won = (await db.execute(q.values(
                status=status, report=report, error=error, finished_at=datetime.now(timezone.utc),
                tool_calls=self.tool_calls, tokens_in=self.tokens_in, tokens_out=self.tokens_out, cost_usd=self.cost,
                skill_generation=self.skill_generation, skill_pins=self.skill_runs,
            ).returning(Investigation.id))).first()
            if won is None:
                self.lost = True
                return False
            await db.execute(delete(InvestigationCheckpoint).where(InvestigationCheckpoint.investigation_id == self.id))
            inv = await db.get(Investigation, self.id)
            await conversations.record_reply(db, inv, (report or {}).get("summary") or error or "No report was produced.")
            await audit.record(db, action="investigation.finish", actor_id=inv.created_by if inv else None,
                               resource_type="investigation", resource_id=str(self.id), namespace=inv.namespace if inv else None,
                               outcome=status, details={"tool_calls": self.tool_calls, "tokens_in": self.tokens_in, "attempt": self.attempt,
                                                        "tokens_out": self.tokens_out, "cost_usd": str(self.cost), "error": error})
        return True

    # ---- checkpoints ----------------------------------------------------------------------

    def _versions(self, llm_cfg: LLMConfig | None) -> dict[str, Any]:
        return {"llm": f"{llm_cfg.provider}/{llm_cfg.model}" if llm_cfg else None, "prompts": hashlib.sha256((prompts.PLANNER_SYSTEM + prompts.REPORT_SYSTEM).encode()).hexdigest()[:16]}

    def _snapshot(self, state: dict[str, Any]) -> dict[str, Any]:
        """Decisions and progress, never evidence payloads or credentials: evidence is re-read from its table."""
        report = state.get("report")
        return {
            "version": CHECKPOINT_VERSION, "completed": self.completed_nodes, "ref": self.ref, "tool_calls": self.tool_calls, "evidence_refs": [e["ref"] for e in self.evidence],
            "graph": {k: (report.model_dump() if k == "report" and report is not None else state.get(k)) for k in ("namespace", "round", "calls", "use_rules", "finished", "mode", "report", "workflow")},
            "request": self.request.model_dump() if self.request else None, "trace": self.trace, "gaps": self.gaps, "retrieval_notes": self.retrieval_notes,
            "sources": [{k: v for k, v in x.items() if k != "text"} for x in self.sources], "shared_findings": self.shared_findings, "excluded": self.excluded,
            "notes": self.notes, "findings": self.findings, "calls_seen": sorted(self.calls_seen), "rejected": self.rejected[-20:], "focus_namespace": self.focus_namespace,
            "skills": [{"digest": sk.digest, "score": score, "reasons": why} for sk, score, why in self.skills], "skill_runs": self.skill_runs, "skill_rejected": self.skill_rejected,
            "skill_generation": self.skill_generation, "connectors": self.connector_ids, "versions": self.versions,
            "budget": {"tool_calls": self.tool_calls, "tokens_in": self.tokens_in, "tokens_out": self.tokens_out, "cost_usd": str(self.cost)},
            "authorization": {"principal": str(self.owner), "checked_at": datetime.now(timezone.utc).isoformat()},
            "scope": {"from": self._window[0].isoformat(), "to": self._window[1].isoformat()} if self._window else None,
        }

    async def _checkpoint(self, node: str, next_node: str, state: dict[str, Any]) -> None:
        self.completed_nodes.append(node)
        body = redact_obj(json.loads(json.dumps(self._snapshot(state), default=str)))
        async with session_scope() as db:
            await self._fence(db)
            values = {"seq": self.seq, "node": node, "next_node": next_node, "attempt": self.attempt, "state": body, "updated_at": func.now()}
            await db.execute(insert(InvestigationCheckpoint).values(investigation_id=self.id, **values).on_conflict_do_update(index_elements=["investigation_id"], set_=values))

    async def _resume(self, llm_cfg: LLMConfig | None) -> dict[str, Any] | None:
        """Restores a checkpoint, after permissions were checked again by the caller. Evidence from before the
        crash stays citable, but anything past the freshness limit becomes historical. Returns graph state."""
        async with session_scope() as db:
            row = await db.get(InvestigationCheckpoint, self.id)
            if row is None:
                return None
            cp, entry, node = row.state, row.next_node, row.node
            if cp.get("version") != CHECKPOINT_VERSION:
                self.notes.append("A checkpoint from another version was ignored; this run started again.")
                return None
            rows = (await db.execute(select(Evidence).where(Evidence.investigation_id == self.id))).scalars().all()
            spent = (await db.execute(select(func.coalesce(func.sum(LLMUsage.tokens_in), 0), func.coalesce(func.sum(LLMUsage.tokens_out), 0), func.coalesce(func.sum(LLMUsage.cost_usd), 0))
                                      .where(LLMUsage.investigation_id == self.id))).one()
            pins = {r.digest: r for r in (await db.execute(select(SkillVersion).where(SkillVersion.digest.in_([p["digest"] for p in cp["skills"]])))).scalars()}
            now = datetime.now(timezone.utc)
            changed = {t for t, i in (cp.get("connectors") or {}).items() if self.connector_ids.get(t) != i}
            aged = [e for e in rows if e.kind in LIVE and ((now - e.collected_at).total_seconds() > self.settings.evidence_max_age_seconds or e.connector_type in changed)]
            await self._fence(db)
            for e in aged:
                e.kind = HISTORICAL
            await db.flush()
            self.evidence = [{"ref": e.ref, "tool": e.tool, "args": e.args, "ok": e.ok, "summary": e.summary, "data": e.data, "error": e.error, "error_kind": None, "denied": e.kind == "access_denied",
                              "source": e.source, "collected_at": e.collected_at.isoformat(), "truncated": e.truncated, "kind": e.kind} for e in sorted(rows, key=lambda r: int(r.ref[1:]))]
            texts = {d: (r.content, r.source, r.skill_id, r.version) for d, r in pins.items()}
        from .. import skills as skill_lib

        self.notes = cp["notes"]
        self.findings = cp["findings"]
        if cp.get("request"):
            self.request = RequestContext.model_validate(cp["request"])
        self.trace = cp.get("trace") or self.trace
        self.gaps, self.retrieval_notes = cp.get("gaps") or [], cp.get("retrieval_notes") or []
        self.shared_findings, self.excluded = cp.get("shared_findings") or [], cp.get("excluded") or []
        by_ref = {e["ref"]: e.get("data") for e in self.evidence}

        def text_of(ref: str, path: str) -> str:  # file text comes back from evidence, not the checkpoint
            d = by_ref.get(ref)
            if isinstance(d, dict):
                return str(d.get("text") or "")
            if isinstance(d, list):
                return "\n".join(f"{h.get('title') or ''}\n{h.get('heading') or ''}\n{h.get('text') or ''}" for h in d if isinstance(h, dict) and (h.get("citation") or {}).get("path") == path)
            return ""
        self.sources = [{**x, "text": text_of(x["ref"], x["path"])} for x in cp.get("sources") or []]
        self.calls_seen = set(cp["calls_seen"]) - {_call_key(e["tool"], e["args"]) for e in self.evidence if e["kind"] == HISTORICAL}
        self.rejected = cp["rejected"]
        self.focus_namespace = cp["focus_namespace"]
        self.completed_nodes = cp["completed"]
        self.ref = max([int(e["ref"][1:]) for e in self.evidence] + [cp["ref"]])
        self.tool_calls = max(cp["tool_calls"], len(self.evidence))  # calls the crash interrupted were still made
        self.tokens_in, self.tokens_out, self.cost = int(spent[0]), int(spent[1]), Decimal(spent[2])
        self.skill_runs, self.skill_rejected, self.skill_generation = cp["skill_runs"], cp["skill_rejected"], cp["skill_generation"]
        self.skills = []
        for pin in cp["skills"]:
            if pin["digest"] in texts:
                content, source, sid, version = texts[pin["digest"]]
                self.skills.append((skill_lib.parse(content, f"registry:{sid}@{version}", source), pin["score"], pin["reasons"]))
            else:
                self.notes.append("A pinned skill version is no longer in the registry, so its guidance was dropped from this run.")
        self.skill_runs = [r for r in self.skill_runs if r["digest"] in texts]
        if changed:
            self.notes.append(f"Resumed with different connectors for {', '.join(sorted(changed))}; evidence read through the old ones is historical.")
        if (cp.get("versions") or {}).get("llm") != self.versions.get("llm") or (cp.get("versions") or {}).get("prompts") != self.versions.get("prompts"):
            self.notes.append("The model or prompts changed between attempts of this investigation.")
        self.resumed_from = node
        await self._emit("note", {"message": f"Resumed after {node} (attempt {self.attempt}). Budgets carry over."})
        graph_state = {k: v for k, v in cp["graph"].items() if v is not None}
        if graph_state.get("report"):
            graph_state["report"] = Report.model_validate(graph_state["report"])
        return {**graph_state, "entry": entry}

    async def _recollect(self, executor: ToolExecutor, namespace: str) -> None:
        """Reads the basics again when a resumed run's evidence has aged. Same budget, same path as any call."""
        stale = [e for e in self.evidence if e["kind"] == HISTORICAL and e["tool"] in RECOLLECT]
        for e in stale[:MAX_RECOLLECT]:
            out = await self._call(executor, e["tool"], e["args"], "read again: the earlier result is too old to rely on")
            if out and out.ok and e["tool"] == "k8s_triage":
                known = {(f["kind"], tuple(f.get("resources", []))): f for f in self.findings}
                for f in out.data.get("findings", []):
                    key = (f["kind"], tuple(f.get("resources", [])))
                    if key in known:
                        known[key]["evidence"] = [*known[key].get("evidence", []), out.evidence_ref]
                    else:
                        self.findings.append({**f, "namespace": e["args"].get("namespace"), "evidence": [out.evidence_ref]})

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
            self.connector_ids = {t: str(c[0].id) if c[0] is not None else None for t, c in connectors.items()}
            llm_cfg = (await db.execute(select(LLMConfig).where(LLMConfig.purpose == "chat", LLMConfig.is_active.is_(True)))).scalar_one_or_none() if self.use_llm else None
            question, namespace, t_from, t_to = inv.question, inv.namespace, inv.time_from, inv.time_to
            self.requested_skill = inv.requested_skill
            principal_id, environment_id = inv.created_by, inv.environment_id
            self.owner = principal_id
            if inv.conversation_id:
                self.history = await conversations.history(db, inv.conversation_id, inv.id, principal, env.name)
                from ..models import Conversation

                conv = await db.get(Conversation, inv.conversation_id)
                self.conversation_namespace = conv.namespace if conv and conv.id != inv.id else None
            queued = (await db.execute(select(InvestigationEvent.payload).where(InvestigationEvent.investigation_id == inv.id, InvestigationEvent.seq == 0))).scalar_one_or_none() or {}
            self.environment_info = {"name": env.name, "is_production": bool(env.is_production), "reason": queued.get("environment_reason"), "namespace_reason": queued.get("namespace_reason")}
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
        self.guard = ctx.guard = AccessGuard(principal_id, environment_id, namespace, P.INVESTIGATION_RUN, ctx)
        await self.guard.check()

        provider = provider_for(llm_cfg) if llm_cfg else None
        self.versions = self._versions(llm_cfg)
        resumed = await self._resume(llm_cfg) if self.attempt > 1 else None
        if resumed and resumed.get("namespace"):
            scope_info["namespace"] = ctx.focus_namespace = resumed["namespace"]
        async with session_scope() as db:
            executor = ToolExecutor(db, ctx, next_ref=self._next_ref, timeout_seconds=min(45, self.settings.max_duration_seconds))
            if resumed:
                await self._recollect(executor, resumed.get("namespace") or namespace)
            final = await graph.run(self, executor, ctx, question, namespace, scope_info, provider, llm_cfg, (LLMError, BudgetExceeded), resumed)
            await db.commit()
        namespace, report = final["namespace"], final["report"]
        mode = final.get("mode") or mode
        req = self.request or build_request(question)
        await self._emit("phase", {"phase": "verification", "label": "Verifying causes against direct evidence"})
        ledger = self._ledger(namespace)
        validate_claims(report, ledger, self.findings, check_support=report.generated_by == "llm")
        self._contracts()
        verify_causes(report, self.findings, ledger, self._withheld())
        await self._emit("phase", {"phase": "report", "label": "Writing findings"})
        payload = report.model_dump()
        payload["scope"] = scope_info
        payload["coverage"] = self._coverage(conn_errors)
        payload["relationships"] = self._relationships()
        payload["primary_resource"] = self._primary_resource()
        payload["findings"] = [f for f in self.findings if (self.request is None or self.request.in_scope(f.get("namespace")) or f.get("namespace") is None)]
        payload["skills"] = [{"name": sk.name, "version": sk.meta.version, "digest": sk.digest, "description": sk.meta.description, "reasons": why} for sk, _, why in self.skills]
        payload["skill_runs"] = self.skill_runs
        payload["skill_rejected"] = self.skill_rejected
        payload["skill_generation"] = self.skill_generation
        payload["skill_actions"] = await self._skill_actions(namespace, env_name)
        payload["notes"] = list(dict.fromkeys([*(payload.get("notes") or []), *self.notes]))
        payload["execution"] = {"attempt": self.attempt, "resumed_after": self.resumed_from, "versions": self.versions}
        payload["shared_findings"] = self.shared_findings
        payload["excluded"] = self.excluded
        payload["request"] = req.trace()
        verdict = relevance.check(payload, req, ledger, self.findings)
        payload["relevance"] = verdict
        self.trace["relevance"] = {"status": verdict["status"], "failed": [c["name"] for c in verdict.get("first_pass", verdict["checks"]) if not c["passed"]], "corrections": verdict["corrections"]}
        self.trace["response_type"] = payload["response_type"]
        payload["trace"] = redact_obj(json.loads(json.dumps({**self.trace, "routing": req.trace()}, default=str)))
        await self._emit("relevance", {"status": verdict["status"], "checks": [{"name": c["name"], "passed": c["passed"]} for c in verdict["checks"]], "corrections": verdict["corrections"]})
        log.info("request answered", extra={"event": "request_answered", "investigation_id": str(self.id), "operation": req.primary, "outcome": verdict["status"],
                                             "response_type": payload["response_type"], "tool_calls": self.tool_calls, "scope": req.scope})
        if not await self._finish("completed", payload, None):
            raise OwnershipLost(str(self.id))
        await self._emit("report", {"report": payload})
        return mode

    def _ledger(self, namespace: str) -> Ledger:
        return Ledger.build(self.evidence, namespace=namespace, max_age_seconds=self.settings.evidence_max_age_seconds)

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
                "tool_calls": self.tool_calls, "tool_budget": self.tool_budget, "budget_reached": self.tool_calls >= self.tool_budget}

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

    def _outside_request(self, tool: str, args: dict[str, Any]) -> str | None:
        """Why a call falls outside what this request needs, if it does. Authorization is the executor's job and
        is unchanged; this narrows further, to the request's workflow and target."""
        from ..tools.builtin import REGISTRY

        req = self.request
        if req is None:
            return None
        allowed = GUIDANCE_TOOLS if req.workflow == "guidance" else CONCEPT_TOOLS if req.workflow == "concept" else None
        if allowed is not None and tool not in allowed and tool not in req.tools_expanded():
            return f"answering a {req.primary.replace('_', ' ')} request does not need {tool}"
        spec = REGISTRY.get(tool)
        if spec is None:
            return None  # the executor refuses unknown tools and audits the attempt
        ns = args.get(spec.namespace_arg) if spec.namespace_arg else None
        if req.workflow == "guidance":
            if tool == "k8s_argocd_status" and not args.get("application"):
                return "guidance reads the target's own Argo CD application, not every application"
            if ns and ns not in req.namespaces and not (tool == "k8s_ingresses" and ns in (req.candidates.get("namespace") or [])):
                return f"namespace {ns} is not established as the target's"
        if req.scope != "target":
            return None
        if ns and ns not in req.namespaces:
            return f"namespace {ns} is outside this request's target ({', '.join(req.namespaces)}); widening needs evidence of a dependency"
        if spec.cluster_scoped and spec.connector_type in ("kubernetes", "prometheus", "loki") and tool not in FILTERED_CLUSTER_TOOLS | req.tools_expanded():
            return f"{tool} reads the whole cluster, and nothing so far shows {req.target_name or req.namespaces[0]} depends on it"
        return None

    async def _call(self, executor: ToolExecutor, tool: str, args: dict[str, Any], purpose: str | None = None) -> ToolOutcome | None:
        key = _call_key(tool, args)
        if key in self.calls_seen:
            return None
        blocked = self._outside_request(tool, args)
        if blocked and not executor.would_deny(tool, args):  # what policy refuses, the executor refuses and audits as before
            self.calls_seen.add(key)
            self.rejected.append({"tool": tool, "args": args, "reason": blocked, "kind": "request_scope"})
            self.trace["scope_rejections"].append({"tool": tool, "namespace": args.get("namespace"), "reason": blocked})
            await executor.refuse(tool, args, blocked)
            await self._emit("note", {"message": f"Not run: {tool} ({blocked})"})
            return None
        if self.tool_calls >= self.tool_budget:
            if "tool budget" not in " ".join(self.notes):
                self.notes.append(f"Tool-call budget of {self.tool_budget} for this request reached.")
            return None
        self.calls_seen.add(key)
        self.tool_calls += 1
        await self._emit("tool_call", {"tool": tool, "args": args, "purpose": purpose})
        out = await executor.run(tool, args)
        try:
            await self._fence(executor.db)  # a result that arrives after the claim was lost is discarded, not stored
        except OwnershipLost:
            await executor.db.rollback()
            await audit.record(None, action="tool.call", resource_type="tool", resource_id=tool, outcome="discarded", details={"investigation_id": str(self.id), "reason": "claim lost before the result was stored"})
            raise
        await executor.db.commit()
        record = {"ref": out.evidence_ref, "tool": tool, "args": out.args, "ok": out.ok, "summary": out.summary,
                  "data": out.data, "error": out.error, "error_kind": out.error_kind, "denied": out.denied,
                  "source": out.source, "collected_at": out.collected_at, "truncated": out.truncated,
                  "kind": out.kind or classify(ok=out.ok, tool=tool, denied=out.denied, truncated=out.truncated)}
        if out.evidence_ref:
            self.evidence.append(record)
        if not out.evidence_ref or out.denied:
            self.rejected.append({"tool": tool, "args": args, "reason": out.error, "kind": out.error_kind})
        await self._emit("tool_result", {k: v for k, v in record.items() if k != "data"} | {"duration_ms": out.duration_ms})
        self.trace["tools"].append({"tool": tool, "purpose": purpose, "namespace": args.get("namespace"), "ref": out.evidence_ref, "ok": out.ok})
        if tool == "k8s_argocd_status" and out.ok and isinstance(out.data, dict):
            self._argo_findings(out)
        return out

    def _argo_findings(self, out: ToolOutcome) -> None:
        """Argo CD reports on every application it manages. Those of the target are findings; others that fail
        for the same concrete cause are a shared failure, kept apart; the rest are left out, with a record of
        what was left out and where it came from. Without a target, apps with one shared cause are grouped."""
        from .. import argocd as argo

        known = {(f["kind"], tuple(f.get("resources", []))) for f in [*self.findings, *self.shared_findings]}
        new = [{**f, "evidence": [out.evidence_ref]} for f in out.data.get("findings", []) if (f["kind"], tuple(f["resources"])) not in known]
        req = self.request
        if req is None or req.scope != "target":
            self.findings.extend(argo.group(new))
            return
        mine = [f for f in new if req.in_scope(f.get("namespace"))]
        others = [f for f in new if not req.in_scope(f.get("namespace"))]
        keys = {((f.get("cause") or {}).get("category"), (f.get("cause") or {}).get("key")) for f in mine if (f.get("cause") or {}).get("key")}
        same = [f for f in others if ((f.get("cause") or {}).get("category"), (f.get("cause") or {}).get("key")) in keys]
        self.findings.extend(mine)
        if same:
            for g in argo.group([*[{**f} for f in mine if ((f.get("cause") or {}).get("category"), (f.get("cause") or {}).get("key")) in keys], *same]):
                if g["kind"] == "argocd_shared_cause":
                    self.shared_findings.append(g)
                    req.expand(None, [], f"{g['title']}: the target's application fails for the same reason, so this is a shared failure", [out.evidence_ref])
        rest = [f for f in others if f not in same]
        if rest:
            self.excluded.append({"tool": "k8s_argocd_status", "evidence": [out.evidence_ref], "count": len(rest), "reason": "Argo CD applications outside this request's target",
                                  "namespaces": sorted({f.get("namespace") or "?" for f in rest})[:20]})

    async def _intake(self, executor: ToolExecutor, ctx: ToolContext, question: str, namespace: str, provider, cfg, failures: tuple) -> None:
        """The request, validated, before any live evidence: intent, target, environment, scope, what is unresolved."""
        from . import guidance

        self.llm_failures = failures
        scores, signals, _ = classify_request(question)
        req = build_request(question, scores=scores, signals=signals)
        if req.confidence == "low" and provider and self.settings.intent_llm_classifier:
            try:
                raw = await self._llm(provider, cfg, LLM_SYSTEM, prompts.untrusted("request", question, 2000), 200, 60)
                req = refine(req, raw, scores)
            except failures as exc:
                self.notes.append(f"The model could not classify the request ({getattr(exc, 'message', str(exc))}); the rules decided.")
        req.environment = ctx.environment.name
        req.environment_reason = self.environment_info.get("reason")
        k8s = ctx.connectors.get("kubernetes")
        req.cluster = (getattr(k8s[0], "name", None) if k8s and k8s[0] is not None else None) or (ctx.environment.name if k8s else None)
        if req.requested_environment and req.requested_environment.startswith("prod") and not self.environment_info.get("is_production"):
            req.ambiguities.append(f"the request names production, but environment {ctx.environment.name} is not marked as production")
        if namespace != "*":
            # The investigation's namespace bounds what may be read. It is the request's target only when the
            # question is about something in particular; "why are Argo CD apps failing" in shop is not about shop alone.
            req.resolve("namespace", namespace, self.environment_info.get("namespace_reason") or "the investigation's namespace")
            pinned = bool(req.targets) or namespace in intent_names(question)
        git = ctx.connectors.get("git")
        repositories = list(getattr(git[1], "repositories", None) or []) if git else []
        names: list[str] | None = None
        if k8s and req.targets:
            try:
                names = ctx.scope.filter(await ctx.connector("kubernetes").list_namespaces())
            except Exception:
                names = None
        guidance.resolve_static(req, repositories, names)
        self.request = req
        if req.workflow == "investigate" and req.targets and not req.resolved("namespace"):
            await guidance.verify_namespace_by_ingress(self, executor, req)
        if req.namespaces and (namespace == "*" or pinned):
            req.scope = "target"
        explicit = [t for t in req.targets if t.kind != "object"]
        if self.history and explicit:
            spoken = " ".join(h["text"] for h in self.history).lower()
            mine = [t.mention.lower() for t in explicit] + ([req.repository.lower()] if req.repository else [])
            if not any(m in spoken for m in mine) or (self.conversation_namespace and req.namespaces and self.conversation_namespace not in ("*", *req.namespaces)):
                self.history, req.history_used = [], False
                self.notes.append(f"Earlier turns of this conversation were about something other than {req.target_name}, so they were left out of this answer.")
        span = trace_api.get_current_span()
        for k, v in (("moks.intent", req.primary), ("moks.workflow", req.workflow), ("moks.response_type", req.response_type), ("moks.scope", req.scope)):
            span.set_attribute(k, v)
        await self._emit("routing", req.trace())
        log.info("request routed", extra={"event": "request_routed", "investigation_id": str(self.id), "operation": req.primary, "workflow": req.workflow,
                                           "scope": req.scope, "unresolved": ",".join(req.unresolved), "classifier": req.classifier})

    async def _shared_dependencies(self, executor: ToolExecutor, ctx: ToolContext) -> None:
        """Widen past the target only when its own evidence points at something shared, and say why."""
        req = self.request
        if req is None or req.scope != "target" or "kubernetes" not in ctx.connectors or not ctx.scope.cluster_wide:
            return
        mine = [e for e in self.evidence if e["ok"] and req.in_scope(e["args"].get("namespace"))]
        dns = [e["ref"] for e in mine if DNS_FAILURE.search(json.dumps(e.get("data"), default=str)[:200_000])]
        if dns and "k8s_dns_health" not in req.tools_expanded():
            req.expand(None, ["k8s_dns_health"], f"{', '.join(dns[:3])} show name-resolution failures in {', '.join(req.namespaces)}, so cluster DNS is a candidate shared dependency", dns[:3])
            await self._emit("note", {"message": f"Scope widened to cluster DNS: {req.expansions[-1]['reason']}"})
            await self._call(executor, "k8s_dns_health", {}, "shared dependency: cluster DNS")
        nodes = [ref for f in self.findings if f["kind"] in ("unschedulable", "evicted") and req.in_scope(f.get("namespace")) for ref in f.get("evidence", [])]
        if nodes and "k8s_nodes" not in req.tools_expanded():
            req.expand(None, ["k8s_nodes"], f"{', '.join(nodes[:3])} show pods that cannot be scheduled or were evicted, so node capacity is shared", nodes[:3])
            await self._call(executor, "k8s_nodes", {}, "shared dependency: node capacity")

    def consider_expansion(self, raw: Any) -> None:
        """A planner's request for more scope stands only on evidence that exists and names what it asks for."""
        req = self.request
        if req is None or req.scope != "target" or not isinstance(raw, dict):
            return
        wanted = [str(n) for n in raw.get("namespaces") or [] if isinstance(n, str)][:3]
        refs = [r for r in raw.get("evidence") or [] if any(e["ref"] == r for e in self.evidence)]
        text = " ".join(json.dumps(e.get("data"), default=str)[:50_000] for e in self.evidence if e["ref"] in refs).lower()
        shown = [n for n in wanted if n in text and n not in req.namespaces]
        if shown:
            req.expand(shown, [], str(raw.get("reason") or "the planner cited a dependency")[:300], refs)
        elif wanted:
            self.trace["scope_rejections"].append({"namespaces": wanted, "reason": "expansion refused: the cited evidence does not name these namespaces"})

    def _view(self, e: dict[str, Any]) -> dict[str, Any]:
        """Evidence as the model sees it for this request: output a tool could not filter is filtered here,
        and the stored evidence keeps everything, so provenance is intact."""
        req = self.request
        if req is None or req.scope != "target" or not e["ok"]:
            return e
        data = e.get("data")
        if e["tool"] == "k8s_argocd_status" and isinstance(data, dict):
            problems = [r for r in data.get("problems") or [] if req.in_scope(r.get("namespace"))]
            removed = len(data.get("problems") or []) - len(problems)
            kept = {**data, "problems": problems, "findings": [f for f in data.get("findings") or [] if req.in_scope(f.get("namespace"))], "healthy": [], "out_of_scope_problems": removed}
            return {**e, "data": kept, "summary": f"{len(problems)} Argo CD application(s) of this target with problems" + (f"; {removed} outside the target left out" if removed else "")}
        if e["tool"] == "prom_alerts" and isinstance(data, list):
            rows, removed = [], 0
            for r in data:
                active = r.get("active") or []
                mine = [x for x in active if req.in_scope((x.get("labels") or {}).get("namespace"))]
                shared = [{**x, "shared": True} for x in active if not (x.get("labels") or {}).get("namespace")]
                removed += len(active) - len(mine) - len(shared)
                if mine or shared:
                    rows.append({**r, "active": mine + shared})
            return {**e, "data": rows, "summary": f"{len(rows)} alert rule(s) for this target or the shared cluster" + (f"; {removed} alert(s) about other namespaces left out" if removed else "")}
        return e

    def _views(self) -> list[dict[str, Any]]:
        return [self._view(e) for e in self.evidence]

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
        req = self.request
        if namespace == "*" and req is not None and req.namespaces:
            res = req.resolved("namespace")
            focus, why = req.namespaces[0], (res.source if res else "resolved from the request")
            self.notes.append(f"Scope: {why}.")
            await self._emit("note", {"message": f"Scope: {why}"})
            async with session_scope() as db:
                await db.execute(update(Investigation).where(Investigation.id == self.id).values(namespace=focus))
            self.focus_namespace = namespace = focus
        elif namespace == "*" and req is not None and req.scope == "cluster":
            self.notes.append("Scope: the request asks about the whole environment.")
        if namespace == "*" and (req is None or req.scope != "cluster"):
            focus, why = await self._focus(ctx, question)
            self.notes.append(f"Scope: {why}.")
            await self._emit("note", {"message": f"Scope: {why}"})
            if focus:
                async with session_scope() as db:
                    await db.execute(update(Investigation).where(Investigation.id == self.id).values(namespace=focus))
                self.focus_namespace = focus
                namespace = focus
                if req is not None:
                    req.resolve("namespace", focus, why)
                    req.scope = "target"
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
        if last.get("reason") == "Unknown":
            # The runtime lost the container, usually in a node restart. The last error line in its log did not cause that.
            self.notes.append(f"{pod}/{container} stopped with reason Unknown (exit {last.get('exitCode')}), which points to a node or runtime restart, not an application failure.")
            return
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

    async def _eligible(self, ctx: ToolContext) -> tuple[dict, dict]:
        """Which skills may run here, decided before ranking. Returns skill name to readiness, and the refused with reasons."""
        from .. import skills as skill_lib
        from ..skills import readiness as rd
        from ..skills import registry
        from ..skills.selection import eligible

        await registry.refresh()
        async with session_scope() as db:
            view = await registry.policy(db)
        prom = ctx.connectors.get("prometheus")
        metrics, complete = (None, False)
        wants = any(sk.meta.telemetry for sk in skill_lib.library().skills.values())
        if wants and prom:
            metrics, complete = await rd.metric_names(str(prom[0].id) if prom[0] is not None else ctx.environment.name, prom[1])
        return eligible(skill_lib.library().skills, ctx.environment.name, view, set(ctx.connectors), metrics, complete, lambda p: ctx.principal.can(P(p), ctx.environment.name, "*"), skill_lib.resources_of(self.findings))

    async def _resolve_owners(self, ctx: ToolContext) -> None:
        """Steps need the Deployment behind a pod. It comes from ownerReferences; a name is only a guess
        (a ReplicaSet called a-b-c need not belong to a Deployment a-b)."""
        k = (ctx.connectors.get("kubernetes") or (None, None))[1]
        if k is None or not hasattr(k, "controller_of"):
            return
        seen: dict[tuple, str | None] = {}
        for f in self.findings:
            kind, _, name = str(f.get("owner") or "").partition("/")
            if kind != "ReplicaSet" or not f.get("namespace") or "controller" in f:
                continue
            key = (f["namespace"], name)
            if key not in seen and len(seen) < MAX_OWNER_LOOKUPS:
                try:
                    seen[key] = await k.controller_of(*key)
                except Exception:
                    seen[key] = None  # unknown, so step arguments fall back and the name stays a guess
            if seen.get(key) is not None:
                f["controller"] = seen[key]

    async def _still_allowed(self, sk, env_name: str) -> str | None:
        """Why a pinned skill must stop now, if it must. Replacing a version does not stop it: only a
        decision against the skill does."""
        from ..skills import registry

        async with session_scope() as db:
            view = await registry.policy(db)
        ok, why = registry.allowed(sk, env_name, view)
        return None if ok else why

    def _repository(self, ctx: ToolContext) -> str:
        git = ctx.connectors.get("git")
        repos = getattr(git[1], "repositories", None) if git else None
        return repos[0] if repos and len(repos) == 1 else ""  # with several, guessing would compare the wrong one

    async def _run_skills(self, executor: ToolExecutor, ctx: ToolContext, namespace: str, question: str) -> None:
        from .. import skills as skill_lib
        from ..skills.registry import snapshot
        from ..tools.builtin import REGISTRY

        await self._resolve_owners(ctx)
        kinds = {f["kind"] for f in self.findings}
        ready, refused = await self._eligible(ctx)
        self.skill_generation = snapshot()["generation"]
        resources = skill_lib.resources_of(self.findings)
        if self.requested_skill and self.requested_skill in refused:
            self.notes.append(f"Skill {self.requested_skill} was asked for but cannot run: {refused[self.requested_skill]}.")
        chosen = skill_lib.select(question, kinds, eligible=lambda sk: sk.name in ready, requested=self.requested_skill, resources=resources)
        rejected = [{"skill": n, "reason": why} for n, why in sorted(refused.items()) if n in {s.name for s, _, _ in skill_lib.select(question, kinds, limit=6)}]
        for r in rejected:
            self.notes.append(f"Skill {r['skill']} not used: {r['reason']}.")
        self.skills = chosen
        self.skill_rejected = rejected
        if not self.skills:
            return
        self.skill_runs = [{"skill": sk.name, "version": sk.meta.version, "digest": sk.digest, "reasons": why, "readiness": ready[sk.name]["state"], "steps": [], "stopped": None}
                           for sk, _, why in self.skills]
        await self._emit("skills", {"skills": [{"name": sk.name, "version": sk.meta.version, "reasons": why} for sk, _, why in self.skills]})
        trace = TRACE_ID.search(question)
        base = {"namespace": "" if namespace == "*" else namespace, "question": question[:200],
                "trace_id": trace.group(0).lower() if trace else "", "repository": self._repository(ctx)}
        budget = max(0, self.tool_budget - self.tool_calls - 4)  # leave room for follow-ups
        # What was asked for runs first, and each skill gets a share, so findings about something else cannot use it all.
        asked = lambda pair: not any(w.startswith(("chosen", "question:")) for w in pair[0][2])
        ordered = sorted(zip(self.skills, self.skill_runs), key=asked)
        for position, ((sk, _, _), run) in enumerate(ordered):
            await self._emit("phase", {"phase": "skill", "label": f"Running skill {sk.name}"})
            share = max(1, budget // (len(ordered) - position))
            budget -= share
            if sk.meta.budget.max_tool_calls is not None and share > sk.meta.budget.max_tool_calls:
                budget += share - sk.meta.budget.max_tool_calls
                share = sk.meta.budget.max_tool_calls
            unmet = self._unmet(sk, base)
            if unmet:
                run["not_run"] = unmet
                run["steps"] = [{"id": s.id, "tool": s.tool, "purpose": s.purpose, "required": s.required, "status": "not_applicable", "reason": unmet, "evidence": [], "errors": []} for s in sk.meta.steps]
                continue
            began, tokens = time.monotonic(), 0
            outcome: dict[str, str] = {}
            for step in sk.meta.steps:
                record = {"id": step.id, "tool": step.tool, "purpose": step.purpose, "required": step.required, "status": "skipped", "reason": None, "evidence": [], "errors": []}
                run["steps"].append(record)
                over = self._over_budget(sk, began, tokens)
                if over:
                    record["reason"] = over
                    run["budget_exhausted"] = True
                    continue
                stop = run["stopped"] or await self._still_allowed(sk, ctx.environment.name)
                if stop:
                    run["stopped"] = stop
                    record["reason"] = f"skill stopped: {stop}"
                    continue
                if step.requires_finding and step.requires_finding not in {f["kind"] for f in self.findings}:
                    record["status"], record["reason"] = "not_applicable", f"needs finding {step.requires_finding}, which this run did not produce"
                    continue
                if step.unless_finding and step.unless_finding in {f["kind"] for f in self.findings}:
                    record["status"], record["reason"] = "not_applicable", f"only runs without finding {step.unless_finding}, which this run produced"
                    continue
                if step.if_step and outcome.get(step.if_step.step) not in step.if_step.status:
                    record["status"], record["reason"] = "not_applicable", f"only runs when {step.if_step.step} is {' or '.join(step.if_step.status)}, and it was {outcome.get(step.if_step.step, 'not run')}"
                    continue
                spec = REGISTRY[step.tool]
                if not all(ctx.available(c) for c in (spec.connector_type, *spec.also_requires)):
                    record["reason"] = f"no {spec.connector_type} connector here"
                    continue
                if spec.cluster_scoped and not ctx.scope.cluster_wide:
                    record["reason"] = "needs access to all namespaces"
                    continue
                if step.when_question and not re.search(step.when_question, question, re.I):
                    record["reason"] = "the question does not call for it"
                    continue
                contexts = skill_lib.contexts_for(step, self.findings, base)
                if not contexts:
                    record["reason"] = f"no matching {step.for_each}"
                    continue
                for c in contexts:
                    args = skill_lib.render(step.args, c)
                    if args is None:
                        missing = sorted(v for v in skill_lib.template_vars(step.args) if not c.get(v))
                        record["reason"] = f"needs {', '.join(missing)}, which this run did not find"
                        continue
                    if share <= 0:
                        record["reason"] = "tool budget used up"
                        run["budget_exhausted"] = True
                        continue
                    seen = len(self.evidence)
                    out = await self._call(executor, step.tool, args, f"[{sk.name}] {step.purpose or step.tool}")
                    if out is None:
                        record["reason"] = record["reason"] or "already collected, or the tool budget is used up"
                        prior = [e for e in self.evidence if e["tool"] == step.tool and e["args"] == args]
                        if prior:  # an earlier attempt that failed is still a failure: a required step must not pass on it
                            worked = any(e["ok"] for e in prior)
                            record.update(status="executed" if worked else "failed", reason="already collected in this run" if worked else "already tried in this run, and it failed")
                            record["evidence"] += [e["ref"] for e in prior]
                            if not worked:
                                record["errors"].append((prior[-1].get("error") or prior[-1].get("summary") or "failed")[:200])
                        continue
                    share -= 1
                    tokens += len(str(out.data or out.summary or "")) // CHARS_PER_TOKEN
                    record["status"] = "executed" if out.ok else "failed"
                    record["evidence"] += [e["ref"] for e in self.evidence[seen:]]
                    if not out.ok:
                        record["errors"].append((out.error or out.summary or "failed")[:200])
                    else:
                        record["reason"] = None
                if step.id:
                    outcome[step.id] = record["status"]
                if step.stop_if_finding and step.stop_if_finding in {f["kind"] for f in self.findings}:
                    run["stopped"], run["escalation"] = f"stop condition met: {step.stop_if_finding}", step.escalate
                    self.notes.append(f"Skill {sk.name} stopped at {step.id or step.tool}: {step.escalate}")
            budget += max(share, 0)  # what one skill did not need goes to the next

    def _unmet(self, sk, base: dict[str, str]) -> str | None:
        """Why a skill should not start. Checked once, before its first step."""
        kinds = {f["kind"] for f in self.findings}
        for c in sk.meta.preconditions:
            if (c.finding in kinds) != (c.expect == "present"):
                return c.label or f"needs finding {c.finding} {c.expect}"
        names = [i.name for i in sk.meta.inputs if i.required and not base.get(i.name) and not any(i.name in ("pod", "workload", "deployment", "service", "resource") and f.get("resources") for f in self.findings)]
        return f"needs {', '.join(names)}, which this question and its findings do not give" if names else None

    @staticmethod
    def _over_budget(sk, began: float, tokens: int) -> str | None:
        b = sk.meta.budget
        if b.max_seconds is not None and time.monotonic() - began > b.max_seconds:
            return f"time budget of {b.max_seconds} seconds used up"
        if b.max_tokens is not None and tokens > b.max_tokens:
            return f"token budget of {b.max_tokens} used up"
        return None

    def _contracts(self) -> None:
        """Holds each skill to what it declared. A plain-text item stays labeled guidance only."""
        kinds = {f["kind"] for f in self.findings}
        for (sk, _, _), run in zip(self.skills, self.skill_runs):
            if run.get("not_run"):
                run.update(checks=[], hypotheses=[], gaps=[], outcome="not_applicable")
                continue
            checks = []
            for item in sk.meta.evidence_required:
                if isinstance(item, str):
                    checks.append({"field": "evidence_required", "text": item, "status": "guidance_only"})
                elif item.when_finding and item.when_finding not in kinds:
                    checks.append({"field": "evidence_required", "text": item.label or item.tool, "status": "not_applicable"})
                else:
                    n = sum(1 for e in self.evidence if e["tool"] == item.tool and e["ok"])
                    met = n >= item.min_ok
                    checks.append({"field": "evidence_required", "text": item.label or f"{item.tool}: at least {item.min_ok}", "status": "passed" if met else "optional_missing" if item.optional else "failed", "observed": n})
            for item in sk.meta.verification:
                if isinstance(item, str):
                    checks.append({"field": "verification", "text": item, "status": "guidance_only"})
                else:
                    present = item.finding in kinds
                    checks.append({"field": "verification", "text": item.label or f"{item.finding} {item.expect}", "status": "passed" if present == (item.expect == "present") else "failed"})
            for item in sk.meta.recovery_validation:
                checks.append({"field": "recovery_validation", "text": item, "status": "guidance_only"} if isinstance(item, str) else {"field": "recovery_validation", "text": item.label or f"{item.finding} {item.expect}", "status": "after_remediation"})
            run["checks"] = checks
            run["hypotheses"] = [self._hypothesis(h, kinds) for h in sk.meta.hypotheses]
            gaps = [f"step {s['tool']} {s['status']}: {s['reason'] or '; '.join(s['errors'])}" for s in run["steps"] if s["required"] and s["status"] not in ("executed", "not_applicable")]
            gaps += [f"check failed: {c['text']}" for c in checks if c["status"] == "failed"]
            run["gaps"] = gaps
            run["outcome"] = "stopped" if run.get("stopped") else "incomplete" if gaps else "complete"  # a required check that did not run is never success
            for g in gaps[:4]:
                self.notes.append(f"Skill {sk.name}: {g}.")

    @staticmethod
    def _hypothesis(h, kinds: set[str]) -> dict:
        """What the run's findings say about a hypothesis. A contradiction outranks support, and this concludes nothing."""
        against, behind = sorted(kinds & set(h.contradicted_by)), sorted(kinds & set(h.supported_by))
        return {"statement": h.statement, "status": "contradicted" if against else "supported" if behind else "untested", "supported_by": behind, "contradicted_by": against}

    def _withheld(self) -> dict[str, str]:
        """Finding kinds a skill took responsibility for and could not back. Missing required evidence stops those
        being reported as verified, whatever the detector saw: the findings stay, as findings."""
        out: dict[str, str] = {}
        for (sk, _, _), run in zip(self.skills, self.skill_runs):
            if run.get("gaps"):
                for kind in sk.meta.triggers.findings:
                    out.setdefault(kind, f"{sk.name} is missing required evidence: {run['gaps'][0]}")
        return out

    async def _skill_actions(self, namespace: str, env_name: str) -> list[dict]:
        """Deterministic, allowlisted remediation suggestions from skills whose `when` matched a finding.
        Suggestions only: turning one into an action plan still needs review (and approval in production)."""
        from .. import skills as skill_lib

        out, seen = [], set()
        finished = {r["skill"]: r for r in self.skill_runs}
        for sk, _, _ in self.skills:
            if await self._still_allowed(sk, env_name) or finished.get(sk.name, {}).get("not_run"):
                continue
            complete = finished.get(sk.name, {}).get("outcome") == "complete"
            recovery = [item.model_dump() for item in sk.meta.recovery_validation if not isinstance(item, str)]
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
                                "finding": f["title"], "evidence": f.get("evidence", []), "evidence_complete": complete, "recovery_checks": recovery})
        return out

    def _capacity(self, cfg: LLMConfig, chars: int, wanted: int, least: int) -> int:
        """Output tokens this call may use: the configured cap, what was asked for, and what the
        investigation has left after the prompt. Raises rather than ask for an answer that cannot fit."""
        left = self.settings.max_tokens_per_investigation - self.tokens_in - self.tokens_out - chars // CHARS_PER_TOKEN
        room = min(cfg.max_output_tokens, wanted, left)
        if room < least:
            raise BudgetExceeded(f"not enough output capacity for this step: {max(room, 0)} tokens left, {least} needed "
                                 f"(configured cap {cfg.max_output_tokens}, investigation budget {self.settings.max_tokens_per_investigation})")
        return room

    async def _llm(self, provider, cfg: LLMConfig, system: str, user: str, wanted: int, least: int) -> dict[str, Any]:
        if self.guard is not None:
            await self.guard.check()  # nothing more goes to the provider for someone who lost access
        messages: list[BaseMessage] = [SystemMessage(content=system), HumanMessage(content=user)]
        last_error: Exception | None = None
        for attempt in range(2):
            room = self._capacity(cfg, sum(len(str(m.content)) for m in messages), wanted, least)
            model = MoksChatModel(provider=provider, max_tokens=room, user_id=self.owner, investigation_id=self.id)
            resp = await model.ainvoke(messages)
            self.tokens_in += resp.usage_metadata["input_tokens"]
            self.tokens_out += resp.usage_metadata["output_tokens"]
            self.cost += Decimal(resp.response_metadata["cost_usd"])
            await self._emit("llm_usage", {"tokens_in": self.tokens_in, "tokens_out": self.tokens_out, "cost_usd": str(self.cost)})
            try:
                return parse_json_object(str(resp.content))
            except (ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                messages = messages + [AIMessage(content=str(resp.content)[:4000]),
                                       HumanMessage(content="That was not a single valid JSON object. Reply again with only the JSON object.")]
        raise LLMError("bad_response", f"model did not return valid JSON ({last_error})")

    async def _plan_round(self, provider, cfg: LLMConfig, question: str, scope_info: dict, round_no: int, ctx: ToolContext) -> dict[str, Any]:
        tools = [t.schema_for_llm() for t in tools_for_planner(ctx)]
        system = prompts.PLANNER_SYSTEM.replace("{max_calls}", str(MAX_CALLS_PER_ROUND)) + prompts.skills_section(self.skills)
        await self._emit("phase", {"phase": "planning", "label": f"Planning follow-up evidence (round {round_no})"})
        return await self._llm(provider, cfg, system, prompts.planner_user(question, scope_info, tools, self._views(), self.findings, self.tool_budget - self.tool_calls, self.rejected, self.request),
                               PLAN_TOKENS, MIN_PLAN_TOKENS)

    async def _act(self, executor: ToolExecutor, ctx: ToolContext, calls: list) -> int:
        """Runs what the planner asked for. Returns how many calls were new."""
        async def call(tool: str, args: dict[str, Any], purpose: str | None) -> ToolOutcome | None:
            return await self._call(executor, tool, args, purpose)

        tools = as_tools(tools_for_planner(ctx), call)
        new = 0
        for item in calls[:MAX_CALLS_PER_ROUND]:
            if not isinstance(item, dict):
                continue
            tool, args = str(item.get("tool", ""))[:80], item.get("args") if isinstance(item.get("args"), dict) else {}
            purpose = str(item.get("purpose", ""))[:120] or None
            if tool not in tools:
                await call(tool, args, purpose)  # still goes through the executor, so the denial is audited and counted
                continue
            if await tools[tool].ainvoke({**args, "__purpose": purpose}) is not None:
                new += 1
        return new

    async def _write_report(self, provider, cfg: LLMConfig | None, question: str, scope_info: dict) -> tuple[Report, str | None]:
        await self._emit("phase", {"phase": "hypotheses", "label": "Forming and checking hypotheses"})
        self.findings = focus(question, self.findings)
        rtype = self.request.response_type if self.request else "troubleshooting"
        if provider:
            try:
                rep = await self._llm_report(provider, cfg, self._with_history(question), scope_info)
                rep.response_type = rtype
                return rep, None
            except (LLMError, BudgetExceeded, ValueError) as exc:
                self.notes.append(f"LLM report unavailable ({getattr(exc, 'message', str(exc))}); showing the rule-based report.")
        rep = rules_report(question, self.findings, self._views(), self.notes)
        rep.response_type = rtype
        return rep, "deterministic" if not provider else "llm_fallback"

    async def _llm_report(self, provider, cfg: LLMConfig, question: str, scope_info: dict) -> Report:
        raw = await self._llm(provider, cfg, prompts.REPORT_SYSTEM + prompts.skills_section(self.skills),
                              prompts.report_user(question, scope_info, self._views(), self.findings, self.notes, self.request, self.shared_findings),
                              REPORT_TOKENS, MIN_REPORT_TOKENS)
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
