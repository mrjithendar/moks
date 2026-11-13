"""What a skill must satisfy before it can be published. Separate from parsing: parsing asks whether
the file is a skill, this asks whether it would work. Errors block publication; warnings do not."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from pydantic import TypeAdapter, ValidationError

if TYPE_CHECKING:
    from . import Skill

def _known_findings() -> set[str]:
    """Everything the code can emit, read from the source so a new detector needs no list kept in step."""
    from pathlib import Path

    root = Path(__file__).parent.parent
    found: set[str] = set()
    for f in [*root.glob("diagnosis/*.py"), *root.glob("orchestrator/*.py"), *root.glob("tools/*.py")]:
        source = f.read_text()
        found |= set(re.findall(r'_finding\(\s*"([a-z_]+)"', source)) | set(re.findall(r'"kind":\s*"([a-z_]+)"', source))
        found |= {x for pair in re.findall(r'"kind":\s*"([a-z_]+)" if [^"]* else "([a-z_]+)"', source) for x in pair}
    from ..datastores import FINDING_KINDS

    return found | set(FINDING_KINDS)


RESOURCE_KINDS = {"Pod", "Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job", "CronJob", "Service", "Ingress", "Node", "Namespace", "PersistentVolumeClaim", "ConfigMap", "Secret", "HPA", "HorizontalPodAutoscaler",
                  "VirtualService", "Gateway", "Application", "Cluster", "Volume", "Alert", "Postgres", "Redis"}
FROM_FINDINGS = {"pod", "workload", "deployment", "service", "resource", "cluster"}  # only a step that iterates over findings can fill these


def _is_finding(name: str, known: set[str]) -> bool:
    return name in known or name.startswith("event_")  # one per Kubernetes warning reason


def _check_args(tool: str, args: dict[str, Any], where: str, add) -> None:
    from ..tools.builtin import REGISTRY
    from . import template_vars

    fields = REGISTRY[tool].args.model_fields
    for name in args:
        if name not in fields:
            add("error", where, f"{tool} has no argument {name}")
    for name, field in fields.items():
        if field.is_required() and name not in args:
            add("error", where, f"{tool} needs argument {name}")
    for name, value in args.items():
        if name not in fields or template_vars(value):
            continue  # a template's value is only known at run time, when the tool validates it
        try:
            TypeAdapter(fields[name].rebuild_annotation()).validate_python(value)
        except ValidationError as exc:
            add("error", where, f"{tool}.{name}: {exc.errors()[0]['msg']}")


def validate(sk: "Skill", others: dict[str, "Skill"] | None = None) -> list[dict[str, str]]:
    from ..actions.catalog import CATALOG
    from ..tools.builtin import REGISTRY
    from . import VARS, EvidenceCheck, VerificationCheck, template_vars

    out: list[dict[str, str]] = []

    def add(level: str, where: str, message: str) -> None:
        out.append({"level": level, "where": where, "message": message})

    known = _known_findings()
    ids = [s.id for s in sk.meta.steps if s.id]
    if len(ids) != len(set(ids)):
        add("error", "steps", "step ids must be unique")
    earlier: set[str] = set()
    for i, step in enumerate(sk.meta.steps):
        where = f"steps[{i}]"
        _check_args(step.tool, step.args, where, add)
        unknown = template_vars(step.args) - VARS
        if unknown:
            add("error", where, f"unknown template variables {sorted(unknown)}")
        if step.if_step and step.if_step.step not in earlier:
            add("error", where, f"if_step names {step.if_step.step}, which is not an earlier step")
        if step.id:
            earlier.add(step.id)
        if step.unless_finding and not _is_finding(step.unless_finding, known):
            add("error", where, f"unless_finding: no detector produces finding {step.unless_finding}")
        if step.requires_finding and step.requires_finding == step.unless_finding:
            add("error", where, "a step cannot both need and refuse the same finding")
        unfilled = template_vars(step.args) & FROM_FINDINGS
        if unfilled and not step.for_each:
            add("warning", where, f"{sorted(unfilled)} come from a finding, and this step does not iterate over one, so it can only be skipped")
        if step.when_question:
            try:
                re.compile(step.when_question)
            except re.error as exc:
                add("error", where, f"when_question is not a valid regular expression: {exc}")
        if step.for_each and not _is_finding(step.for_each.split(".", 1)[1], known):
            add("warning", where, f"no detector produces {step.for_each}")
        for label, name in (("requires_finding", step.requires_finding), ("stop_if_finding", step.stop_if_finding)):
            if name and not _is_finding(name, known):
                add("error", where, f"{label}: no detector produces finding {name}")
        if step.stop_if_finding and not step.escalate:
            add("error", where, "a stop condition must say how to escalate")
        if step.required and step.when_question:
            add("warning", where, "a required step that depends on the question can be skipped and reported as a gap")
    for f in sk.meta.triggers.findings:
        if not _is_finding(f, known):
            add("warning", "triggers", f"no detector produces finding {f}")
    for i, r in enumerate(sk.meta.remediation):
        where = f"remediation[{i}]"
        if not _is_finding(r.when, known):
            add("warning", where, f"no detector produces finding {r.when}")
        unknown = template_vars(r.target) - VARS
        if unknown:
            add("error", where, f"unknown template variables {sorted(unknown)}")
        fields = CATALOG[r.action].params.model_fields
        for name in r.params:
            if name not in fields:
                add("error", where, f"{r.action} has no parameter {name}")
        kind = r.target.partition("/")[0] if "/" in r.target else None
        if kind and kind not in CATALOG[r.action].target_kinds:
            add("error", where, f"{r.action} acts on {', '.join(CATALOG[r.action].target_kinds)}, not {kind}")
        missing = [name for name, f in fields.items() if f.is_required() and name not in r.params]
        if missing:
            add("info", where, f"{r.action} needs {', '.join(missing)} from the finding when proposed; without it nothing is suggested")
    for i, item in enumerate(sk.meta.evidence_required):
        if isinstance(item, EvidenceCheck) and item.tool not in REGISTRY:
            add("error", f"evidence_required[{i}]", f"unknown tool {item.tool}")
        if isinstance(item, str):
            add("info", f"evidence_required[{i}]", "guidance only: not checked by MOKS")
    for i, item in enumerate(sk.meta.verification):
        if isinstance(item, VerificationCheck) and not _is_finding(item.finding, known):
            add("error", f"verification[{i}]", f"no detector produces finding {item.finding}")
        if isinstance(item, str):
            add("info", f"verification[{i}]", "guidance only: not checked by MOKS")
    for i, item in enumerate(sk.meta.recovery_validation):
        if isinstance(item, VerificationCheck) and not _is_finding(item.finding, known):
            add("error", f"recovery_validation[{i}]", f"no detector produces finding {item.finding}")
        if isinstance(item, str):
            add("info", f"recovery_validation[{i}]", "guidance only: not checked by MOKS")
    for i, h in enumerate(sk.meta.hypotheses):
        for name in [*h.supported_by, *h.contradicted_by]:
            if not _is_finding(name, known):
                add("error", f"hypotheses[{i}]", f"no detector produces finding {name}")
    for i, c in enumerate(sk.meta.preconditions):
        if not _is_finding(c.finding, known):
            add("error", f"preconditions[{i}]", f"no detector produces finding {c.finding}")
    for i, item in enumerate(sk.meta.inputs):
        if item.name not in VARS:
            add("error", f"inputs[{i}]", f"{item.name} is not something MOKS can supply")
    for kind in sk.meta.resource_types:
        if kind not in RESOURCE_KINDS and not kind[:1].islower():
            add("warning", "resource_types", f"{kind} is not a resource kind MOKS recognises, so it can never match")
    for pattern in sk.meta.environments:
        if not re.fullmatch(r"[a-z0-9*?\-]{1,100}", pattern):
            add("error", "environments", f"{pattern!r} is not an environment pattern")
    if not sk.meta.environments:
        add("error", "environments", "a skill with no environments can never run; use ['*'] for all")
    for name, q in sk.meta.queries.items():
        unknown = template_vars(q.query) - VARS
        if unknown:
            add("warning", f"queries.{name}", f"placeholders {sorted(unknown)} are not filled in by MOKS")
    seen: dict[str, str] = {}
    for rule in sk.meta.detection:
        if rule.id in seen:
            add("error", "detection", f"rule {rule.id} appears twice")
        seen[rule.id] = sk.name
        if rule.kind in ("prometheus_alert", "cloudwatch_alarm"):
            try:
                re.compile(rule.query or "")
            except re.error as exc:
                add("error", f"detection.{rule.id}", f"query is not a valid regular expression: {exc}")
        if rule.kind == "finding" and not _is_finding(rule.finding or "", known):
            add("error", f"detection.{rule.id}", f"no detector produces finding {rule.finding}")
        if rule.kind == "datastore":
            from ..datastores import FINDING_KINDS

            if rule.finding not in FINDING_KINDS:
                add("error", f"detection.{rule.id}", f"no data-store check produces finding {rule.finding}")
    for other in (others or {}).values():
        if other.name != sk.name:
            for rule in other.meta.detection:
                if rule.id in seen:
                    add("error", "detection", f"rule {rule.id} is also declared by {other.name}")
    return out


def validate_set(skills: dict[str, "Skill"]) -> list[dict[str, str]]:
    """The complete proposed generation, as one thing: each skill against all the others."""
    issues: list[dict[str, str]] = []
    for sk in skills.values():
        issues += [{**i, "skill": sk.name, "version": sk.meta.version} for i in validate(sk, skills)]
    return issues


def errors(issues: list[dict[str, str]]) -> list[str]:
    return [i["message"] for i in issues if i["level"] == "error"]
