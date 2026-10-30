"""SRE skills: reusable investigation playbooks written as SKILL.md files.

A skill is a directory containing SKILL.md: YAML frontmatter followed by Markdown guidance.

    ---
    name: crashloop-triage
    description: One line: what the skill diagnoses.
    triggers:
      keywords: [crash, crashloop, restarting]     # matched (whole words, case-insensitive) in the question
      findings: [crashloop]                        # detector finding kinds that activate it
    steps:                                         # read-only tool calls, run in order (rules and LLM mode)
      - tool: k8s_pod_logs
        for_each: finding.crashloop                # one call per matching finding (or omit: one call)
        limit: 2
        args: {namespace: "{namespace}", pod: "{pod}", previous: true, tail_lines: 150}
        purpose: logs of the crashed container
    remediation:                                   # allowlisted action hints for the report
      - when: crashloop
        action: rollback_deployment
        target: "Deployment/{deployment}"
        note: Roll back if a rollout preceded the first crash.
    ---
    # Markdown runbook: what to check, how to interpret it, what not to conclude.

Trust model: skills are operator-authored and loaded only from the image (moks/skills/builtin) and
MOKS_SKILLS_DIR (a reviewed ConfigMap), never from retrieved documents or tool output. Steps may
only name registered read-only tools; every call still goes through the ToolExecutor (RBAC, scope,
bounds, audit). Remediation hints may only name allowlisted actions and never execute anything.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

log = logging.getLogger("moks.skills")
BUILTIN = Path(__file__).parent / "builtin"
VARS = {"namespace", "pod", "workload", "deployment", "service", "question", "resource", "trace_id"}
_VAR = re.compile(r"\{([a-z_]+)\}")


class StepSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    for_each: str | None = Field(default=None, pattern=r"^finding\.[a-z_]+$")
    limit: int = Field(default=1, ge=1, le=5)
    purpose: str | None = Field(default=None, max_length=120)
    when_question: str | None = Field(default=None, max_length=200, description="regex; run only if the question matches")


class RemediationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    when: str
    action: str
    target: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    note: str = Field(default="", max_length=400)


class Triggers(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keywords: list[str] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)


class DetectionSpec(BaseModel):
    """A deterministic rule. Rules read evidence; they cannot name an action, so an incident never
    authorizes a change: remediation stays behind action plans and approvals."""
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,80}$")
    kind: Literal["finding", "prometheus", "prometheus_alert", "loki", "cloudwatch_alarm"]
    finding: str | None = Field(default=None, pattern=r"^[a-z_]+$")   # kind finding
    query: str | None = Field(default=None, max_length=2000)          # prometheus: PromQL; loki: text; alert kinds: name regex
    resource_label: list[str] = Field(default_factory=list, max_length=4)
    operator: Literal[">", ">=", "<", "<=", "==", "!="] = ">"
    threshold: float = 0
    enabled: bool = True
    environments: list[str] = Field(default_factory=lambda: ["*"])
    namespaces: list[str] = Field(default_factory=lambda: ["*"])
    interval_minutes: int = Field(default=5, ge=1, le=1440)
    lookback_minutes: int = Field(default=15, ge=1, le=1440)
    sustained_checks: int = Field(default=2, ge=1, le=20)
    recovery_checks: int = Field(default=3, ge=1, le=50)
    severity: Literal["sev1", "sev2", "sev3", "sev4"] = "sev3"
    min_evidence: int = Field(default=1, ge=1, le=20)
    cooldown_minutes: int = Field(default=30, ge=0, le=1440)
    owner: str | None = Field(default=None, max_length=120)
    auto_investigate: bool = False
    auto_resolve: bool = False
    empty_means_ok: bool = False  # no results proves health only when the rule says so
    title: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _shape(self) -> "DetectionSpec":
        if self.kind == "finding" and not self.finding:
            raise ValueError(f"{self.id}: a finding rule needs finding")
        if self.kind != "finding" and not self.query:
            raise ValueError(f"{self.id}: a {self.kind} rule needs query")
        return self


class QueryTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: Literal["promql", "logql", "cloudwatch"]
    query: str = Field(max_length=2000)
    purpose: str = Field(default="", max_length=200)


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    statement: str = Field(max_length=300)
    supports: str = Field(default="", max_length=300)
    refutes: str = Field(default="", max_length=300)


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fixture: str = Field(max_length=200)
    expect: str = Field(max_length=200)


class SkillMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,60}$")
    description: str = Field(min_length=10, max_length=400)
    triggers: Triggers = Field(default_factory=Triggers)
    steps: list[StepSpec] = Field(default_factory=list, max_length=12)
    remediation: list[RemediationSpec] = Field(default_factory=list, max_length=8)
    version: str = "1"
    title: str | None = Field(default=None, max_length=120)
    owner: str = Field(default="sre", max_length=120)
    category: Literal["kubernetes", "application", "aws", "devops", "support", "finops"] = "kubernetes"
    resource_types: list[str] = Field(default_factory=list, max_length=12)
    requires: list[str] = Field(default_factory=list, max_length=6)   # connectors, beyond those its steps need
    telemetry: list[str] = Field(default_factory=list, max_length=12)  # metrics that must exist
    detection: list[DetectionSpec] = Field(default_factory=list, max_length=6)
    queries: dict[str, QueryTemplate] = Field(default_factory=dict, max_length=12)
    hypotheses: list[Hypothesis] = Field(default_factory=list, max_length=8)
    evidence_required: list[str] = Field(default_factory=list, max_length=10)
    verification: list[str] = Field(default_factory=list, max_length=8)
    recovery_validation: list[str] = Field(default_factory=list, max_length=8)
    runbooks: list[str] = Field(default_factory=list, max_length=8)
    evaluation: list[EvalCase] = Field(default_factory=list, max_length=8)

    def needs(self) -> set[str]:
        from ..tools.builtin import REGISTRY
        from ..tools.contracts import PSEUDO_CONNECTORS

        tools = {REGISTRY[s.tool].connector_type for s in self.steps}
        return {c for c in (tools | set(self.requires)) if c not in PSEUDO_CONNECTORS}

    @field_validator("steps")
    @classmethod
    def _tools_are_read_only(cls, steps: list[StepSpec]) -> list[StepSpec]:
        from ..tools.builtin import REGISTRY

        for s in steps:
            spec = REGISTRY.get(s.tool)
            if spec is None:
                raise ValueError(f"unknown tool {s.tool!r}")
            if not spec.read_only or spec.privileged:
                raise ValueError(f"{s.tool} is not a read-only tool; skills cannot mutate")
            unknown = {v for val in s.args.values() if isinstance(val, str) for v in _VAR.findall(val)} - VARS
            if unknown:
                raise ValueError(f"unknown template variables in {s.tool}: {sorted(unknown)}")
        return steps

    @field_validator("remediation")
    @classmethod
    def _actions_allowlisted(cls, rem: list[RemediationSpec]) -> list[RemediationSpec]:
        from ..actions.catalog import CATALOG

        for r in rem:
            if r.action not in CATALOG:
                raise ValueError(f"remediation action {r.action!r} is not allowlisted")
        return rem


@dataclass
class Skill:
    meta: SkillMeta
    body: str
    path: str
    source: str  # builtin | custom
    keyword_res: list[re.Pattern] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.meta.name


@dataclass
class SkillLibrary:
    skills: dict[str, Skill]
    errors: list[dict[str, str]]


def parse(text: str, path: str, source: str) -> Skill:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        raise ValueError("SKILL.md must start with YAML frontmatter between --- lines")
    try:
        data = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML frontmatter: {exc}") from None
    try:
        meta = SkillMeta.model_validate(data)
    except ValidationError as exc:
        e = exc.errors()[0]
        raise ValueError(f"{'.'.join(map(str, e['loc']))}: {e['msg']}") from None
    body = m.group(2).strip()
    if len(body) > 12_000:
        raise ValueError("guidance body longer than 12,000 characters")
    kws = [re.compile(rf"(?i)\b{re.escape(k)}\w*") for k in meta.triggers.keywords]
    return Skill(meta, body, path, source, kws)


def availability(sk: "Skill", connectors: set[str], metrics: set[str] | None) -> dict[str, Any]:
    """Whether a skill can work in an environment, and if not, why. metrics None: not known."""
    missing = sorted(sk.meta.needs() - connectors)
    gaps = sorted(set(sk.meta.telemetry) - metrics) if metrics is not None else []
    reasons = [f"needs the {c} connector" for c in missing] + [f"needs metric {m}" for m in gaps]
    return {"available": not reasons, "reasons": reasons}


def load(extra_dir: str | None = None) -> SkillLibrary:
    skills: dict[str, Skill] = {}
    errors: list[dict[str, str]] = []
    dirs = [(BUILTIN, "builtin")]
    extra = extra_dir if extra_dir is not None else os.environ.get("MOKS_SKILLS_DIR", "")
    if extra:
        dirs.append((Path(extra), "custom"))
    for base, source in dirs:
        if not base.is_dir():
            continue
        for f in sorted(base.glob("*/SKILL.md")) + sorted(base.glob("*.skill.md")):
            try:
                sk = parse(f.read_text(encoding="utf-8"), str(f), source)
                if sk.name in skills and source == "builtin":
                    raise ValueError(f"duplicate skill name {sk.name}")
                skills[sk.name] = sk  # custom skills may override built-ins by name
            except (ValueError, OSError) as exc:
                errors.append({"path": str(f), "error": str(exc)})
                log.warning("skill rejected", extra={"event": "skill_rejected", "errorType": "ValueError"})
    return SkillLibrary(skills, errors)


_library: SkillLibrary | None = None


def library() -> SkillLibrary:
    global _library
    if _library is None:
        _library = load()
    return _library


def reload() -> SkillLibrary:
    global _library
    _library = load()
    return _library


def select(question: str, finding_kinds: set[str], limit: int = 3) -> list[tuple[Skill, int, list[str]]]:
    """Rank skills by evidence: matching detector findings weigh more than question keywords."""
    ranked = []
    for sk in library().skills.values():
        reasons = []
        score = 0
        hit_kinds = sorted(finding_kinds & set(sk.meta.triggers.findings))
        if hit_kinds:
            score += 3 * len(hit_kinds)
            reasons.append("findings: " + ", ".join(hit_kinds))
        words = sorted({m.group(0).lower() for r in sk.keyword_res for m in [r.search(question)] if m})
        if words:
            score += len(words)
            reasons.append("question: " + ", ".join(words))
        if score:
            ranked.append((sk, score, reasons))
    ranked.sort(key=lambda t: (-t[1], t[0].name))
    return ranked[:limit]


def render(value: Any, ctx: dict[str, str]) -> Any:
    """Fill {var} placeholders; returns None when a needed variable is missing (step is skipped)."""
    if isinstance(value, str):
        missing = [v for v in _VAR.findall(value) if not ctx.get(v)]
        if missing:
            return None
        return _VAR.sub(lambda m: ctx[m.group(1)], value)
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            r = render(v, ctx)
            if r is None:
                return None
            out[k] = r
        return out
    return value


def contexts_for(step: StepSpec, findings: list[dict], base: dict[str, str]) -> list[dict[str, str]]:
    if not step.for_each:
        return [dict(base)]
    kind = step.for_each.split(".", 1)[1]
    out = []
    for f in findings:
        if f.get("kind") != kind:
            continue
        for res in f.get("resources") or []:
            rkind, _, rname = res.partition("/")
            ctx = dict(base, namespace=f.get("namespace") or base.get("namespace", ""), resource=res)
            if rkind == "pod":
                ctx["pod"] = rname
                owner = f.get("owner") or ""
                okind, _, oname = owner.partition("/")
                if okind == "ReplicaSet" and oname:
                    ctx["deployment"] = oname.rsplit("-", 1)[0]
                    ctx["workload"] = ctx["deployment"]
                elif oname:
                    ctx["workload"] = oname
            elif rkind == "deployment":
                ctx["deployment"] = ctx["workload"] = rname
            elif rkind == "service":
                ctx["service"] = rname
            out.append(ctx)
    return out[: step.limit]
