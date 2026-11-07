"""Three-way comparison against the last verified baseline, and what a chosen resolution would write.

Both functions work on side states (existence, version identifiers, type, per-key keyed digests) and never on
values, so the API can recompute an item when a reviewer picks a resolution, and the executor reaches the same
answer when it checks the item before writing.

A winner is never picked by timestamp. When both sides moved away from the baseline, or there is no baseline yet
and the sides differ, nothing is proposed: the reviewer chooses Kubernetes, AWS, or a per-key edit.
"""
from __future__ import annotations

from typing import Any

from .content import FORMATS, content_digest, diff, merge
from .ownership import write_refusal

CLASSIFICATIONS = {
    "in_sync": "In sync",
    "changed_kubernetes": "Changed in Kubernetes only",
    "changed_aws": "Changed in AWS only",
    "changed_both": "Changed on both sides",
    "missing_kubernetes": "Missing in Kubernetes",
    "missing_aws": "Missing in AWS",
    "unsupported": "Unmanaged or unsupported",
    "failed": "Access denied or comparison failed",
}
SIDES = ("aws", "kubernetes")
DIRECTION_ALLOWS = {"export": {"aws"}, "import": {"kubernetes"}, "both": {"aws", "kubernetes"}}


def classify(kube: dict[str, Any], aws: dict[str, Any], baseline: dict[str, Any] | None, kid: str) -> tuple[str, bool, str | None, str | None]:
    """(classification, first_sync, proposed source side, reason)."""
    for side, st in (("Kubernetes", kube), ("AWS", aws)):
        if st.get("error"):
            return "failed", False, None, f"{side}: {st['error']}"
    for side, st in (("Kubernetes", kube), ("AWS", aws)):
        if st.get("exists") and st.get("unsupported"):
            return "unsupported", False, None, f"{side}: {st['unsupported']}"
    if not kube.get("exists") and not aws.get("exists"):
        return "unsupported", False, None, "neither side exists"
    known = bool(baseline) and baseline.get("kid") == kid
    if not kube.get("exists"):
        return "missing_kubernetes", not known, "aws", None
    if not aws.get("exists"):
        return "missing_aws", not known, "kubernetes", None
    k, a = kube["digest"], aws["digest"]
    if k == a:
        return "in_sync", False, None, None
    if not known:
        return "changed_both", True, None, "no verified baseline yet, and the two sides differ: choose the source"
    b = baseline["digest"]  # type: ignore[index]
    if k == b:
        return "changed_aws", False, "aws", None
    if a == b:
        return "changed_kubernetes", False, "kubernetes", None
    return "changed_both", False, None, "both sides changed since the last verified sync: choose the source or edit per key"


def format_refusal(fmt: str, key_mapping: dict[str, Any], keys: dict[str, dict[str, Any]], type_: str, mapping_type: str) -> str | None:
    """Why this content cannot be written to AWS in the mapping's format, judged from key summaries."""
    if fmt not in FORMATS:
        return f"unknown payload format {fmt}"
    if fmt != "moks-k8s-v1" and type_ != mapping_type:
        return f"the {fmt} format does not store the Secret type, and this Secret is {type_} while the mapping says {mapping_type}"
    if fmt in ("json", "text"):
        binary = sorted(k for k, v in keys.items() if v.get("binary"))
        if binary:
            return f"keys {', '.join(binary)} hold binary data, which the {fmt} format cannot carry; use moks-k8s-v1"
    if fmt in ("text", "binary"):
        key = (key_mapping or {}).get("key")
        if set(keys) != {key}:
            return f"the {fmt} format carries exactly one key ({key}); this content has {', '.join(sorted(keys)) or 'none'}"
    if fmt == "json" and (key_mapping or {}).get("keys"):
        carried = set(key_mapping["keys"].values())
        outside = sorted(set(keys) - carried)
        if outside:
            return f"keys {', '.join(outside)} are not in the mapping's key list"
    return None


def derive(*, kind: str, mapping: dict[str, Any], kube: dict[str, Any], aws: dict[str, Any], source: dict[str, Any] | None,
           classification: str, proposal: str | None, resolution: str | None, key_choices: dict[str, str], targets_requested: list[str] | None,
           aws_allow_delete: bool) -> dict[str, Any]:
    """targets, actions, expected content, per-target diff, warnings, status and the reason it cannot run (blocked).
    mapping: direction, payload_format, key_mapping, k8s_type, expected_owner, management_policy."""
    out: dict[str, Any] = {"targets": [], "actions": {}, "expected": {}, "diff": {}, "warnings": [], "blocked": None, "status": "pending"}
    states = {"aws": aws, "kubernetes": kube}

    if kind == "delete":
        targets = [t for t in targets_requested or [] if t in SIDES]
        for t in targets:
            if not states[t].get("exists"):
                out["blocked"] = f"nothing to delete in {t}"
            out["diff"][t] = diff({}, states[t].get("keys") or {})
        if "aws" in targets and not aws_allow_delete:
            out["blocked"] = "the AWS connector does not allow deletion (config.allow_delete)"
        out.update(targets=targets, actions={t: "delete" for t in targets}, expected={"absent": True})
        _ownership(out, mapping, states, targets)
        return _finish(out)

    if kind == "rollback":
        if not source or not source.get("exists"):
            out["blocked"] = "the version to roll back to could not be read"
            return _finish(out)
        expected = {"type": source["type"], "keys": source["keys"], "digest": source["digest"]}
        targets = [t for t in targets_requested or ["aws"] if t in SIDES]
        actions = {}
        for t in targets:
            st = states[t]
            if t == "aws":
                actions[t] = "restage" if st.get("version_id") != source.get("version_id") else "none"
            else:
                actions[t] = "none" if st.get("exists") and st.get("digest") == expected["digest"] else ("update" if st.get("exists") else "create")
        out.update(targets=[t for t in targets if actions[t] != "none"], actions={t: a for t, a in actions.items() if a != "none"}, expected=expected)
        if not out["targets"]:
            out["status"] = "skipped"
            out["blocked"] = "the destinations already hold this version"
            return _finish(out)
        _writes(out, mapping, states, expected)
        return _finish(out)

    # sync
    if classification in ("failed", "unsupported"):
        out["status"] = "skipped"
        out["blocked"] = "this mapping cannot be synced until the comparison succeeds"
        return _finish(out)
    if classification == "in_sync" and resolution is None:
        out["status"] = "skipped"
        return _finish(out)
    chosen = resolution or (proposal if classification != "changed_both" else None)
    if chosen is None:
        out["status"] = "conflict"
        out["blocked"] = "choose Kubernetes, AWS or an edited resolution"
        return _finish(out)
    if chosen == "edited":
        try:
            keys = merge(kube.get("keys") or {}, aws.get("keys") or {}, key_choices or {})
        except Exception as exc:  # FormatError: a key has no choice, or the choice names a side without the key
            out["status"] = "conflict"
            out["blocked"] = getattr(exc, "message", str(exc))
            return _finish(out)
        type_ = kube.get("type") or aws.get("type") or mapping["k8s_type"]
        expected = {"type": type_, "keys": keys, "digest": content_digest(type_, keys)}
        targets = [t for t in SIDES if not states[t].get("exists") or states[t].get("digest") != expected["digest"]]
    else:
        src = states[chosen]
        if not src.get("exists"):
            out["status"] = "conflict"
            out["blocked"] = f"{chosen} has no secret to copy from"
            return _finish(out)
        expected = {"type": src["type"], "keys": src["keys"], "digest": src["digest"]}
        other = "aws" if chosen == "kubernetes" else "kubernetes"
        targets = [other] if not states[other].get("exists") or states[other].get("digest") != expected["digest"] else []
    if not targets:
        out["status"] = "skipped"
        out["expected"] = expected
        return _finish(out)
    out.update(targets=targets, actions={t: "update" if states[t].get("exists") else "create" for t in targets}, expected=expected)
    _writes(out, mapping, states, expected)
    return _finish(out)


def _writes(out: dict[str, Any], mapping: dict[str, Any], states: dict[str, dict[str, Any]], expected: dict[str, Any]) -> None:
    allowed = DIRECTION_ALLOWS[mapping["direction"]]
    for t in out["targets"]:
        st = states[t]
        out["diff"][t] = {**diff(expected["keys"], st.get("keys") if st.get("exists") else {}),
                          "type_change": None if not st.get("exists") or st.get("type") == expected["type"] else {"from": st.get("type"), "to": expected["type"]}}
        if t not in allowed:
            out["blocked"] = f"the mapping's direction is {mapping['direction']}, which never writes to {t}"
        if t == "aws":
            why = format_refusal(mapping["payload_format"], mapping.get("key_mapping") or {}, expected["keys"], expected["type"], mapping["k8s_type"])
            if why:
                out["blocked"] = why
            if st.get("deleted_date"):
                out["blocked"] = "the AWS secret is scheduled for deletion; restore it in AWS (RestoreSecret) before syncing"
            if st.get("rotation_enabled"):
                out["warnings"].append("rotation is enabled on the AWS secret: the next rotation will replace what MOKS writes")
        else:
            if st.get("exists") and st.get("type") != expected["type"]:
                out["blocked"] = f"Kubernetes does not allow changing a Secret's type ({st.get('type')} to {expected['type']}); delete it through a deletion plan and recreate it"
            if st.get("exists") and st.get("immutable"):
                out["blocked"] = "the Secret is immutable; delete it through a deletion plan and recreate it"
            if st.get("last_applied"):
                out["warnings"].append("this Secret carries kubectl's last-applied-configuration, a copy of its old data: a later kubectl apply can restore the old values")
    _ownership(out, mapping, states, out["targets"])


def _ownership(out: dict[str, Any], mapping: dict[str, Any], states: dict[str, dict[str, Any]], targets: list[str]) -> None:
    for t in targets:
        found = (states[t].get("owner") or {}).get("manager") if states[t].get("exists") else None
        why = write_refusal(found, (mapping.get("expected_owner") or {}).get(t), mapping.get("management_policy", "refuse_managed"))
        if why:
            out["blocked"] = why
        elif found not in (None, "moks"):
            out["warnings"].append(f"{found} manages this {t} secret and may revert or overwrite MOKS's change")


def _finish(out: dict[str, Any]) -> dict[str, Any]:
    if out["blocked"] and out["status"] == "pending":
        out["status"] = "skipped"
    return out
