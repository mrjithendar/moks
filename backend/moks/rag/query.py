"""What a question asks for, read without a model. Everything here only shapes ranking: access is decided
by the grants passed to search, which nothing here can see or change, so no rewrite can widen a scope."""
from __future__ import annotations

import re

ERROR = re.compile(r"\b(?:[A-Z]{2,}_[A-Z0-9_]+|[A-Z][a-z]+(?:[A-Z][a-z]+)*(?:Error|Exception|BackOff|Killed|Failed)|(?:HTTP\s*)?[45]\d{2}\b|E[A-Z]{3,})")
RESOURCE = re.compile(r"(?i)\b(pods?|deployments?|services?|ingress(?:es)?|nodes?|statefulsets?|daemonsets?|pvcs?|configmaps?|secrets?|namespaces?|jobs?|cronjobs?)\b")
TIME = re.compile(r"(?i)\b(today|yesterday|last (?:night|week|hour|\d+ (?:minutes?|hours?|days?))|since \S+|\d{1,2}:\d{2}|\d{4}-\d{2}-\d{2})\b")
CLAUSE = re.compile(r"(?i)\?\s+|;\s+|\s+and also\s+|\s+as well as\s+")
MAX_PARTS = 3
MIN_WORDS = 3


def understand(query: str, identifiers: list[str]) -> dict:
    return {"identifiers": identifiers, "errors": list(dict.fromkeys(ERROR.findall(query)))[:6],
            "resource_types": sorted({m.lower().rstrip("s") for m in RESOURCE.findall(query)})[:6], "time_references": TIME.findall(query)[:4]}


def parts(query: str) -> list[str]:
    """Separate questions in one sentence. A whole query averages them out, and each deserves its own ranking."""
    found = [p.strip(" ?") for p in CLAUSE.split(query)]
    found = [p for p in found if len(p.split()) >= MIN_WORDS]
    return found[:MAX_PARTS] if len(found) > 1 else []
