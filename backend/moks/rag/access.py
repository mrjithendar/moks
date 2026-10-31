"""Who may retrieve which knowledge.

A document carries an environment and a namespace, from its source. "*" on the document means that
axis is unrestricted, so (*, *) is shared with everyone who can read knowledge at all; a source only
gets that scope by an explicit decision (KnowledgeSource.shared_globally).

A person reads a document when one of their bindings grants investigation.read and that single
binding matches the document on both axes. Environment and namespace must be judged together: someone
with prod/team-a and dev/team-b may not read prod/team-b. So the filter is a list of whole grants, never
one list of environments and another of namespaces.

The filter runs before ranking wherever the backend can express it, and every result is checked again
against PostgreSQL by can_read. A backend that cannot express a pattern says so (exact=False), and
search widens its candidate pool within a bound rather than returning too few.
"""
from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase

from ..rbac import ROLES, P, Principal

PERM = P.INVESTIGATION_READ
GLOB = "*?["


@dataclass(frozen=True)
class Grant:
    environment: str  # a value or a pattern, from one binding
    namespace: str


def can_read(principal: Principal, environment: str, namespace: str) -> bool:
    return any(_covers(g, environment, namespace) for g in grants(principal))


def _covers(g: Grant, environment: str, namespace: str) -> bool:
    return (environment == "*" or fnmatchcase(environment, g.environment)) and (namespace == "*" or fnmatchcase(namespace, g.namespace))


def grants(principal: Principal, environment: str | None = None, namespace: str | None = None) -> list[Grant]:
    """The bindings that give knowledge access, narrowed to a requested environment and namespace.
    Asking for an environment or namespace you hold no binding on gives no grants, and so no results,
    even for shared documents: the request is about a scope you cannot see."""
    out: dict[Grant, None] = {}
    for b in principal.bindings:
        if PERM not in ROLES.get(b.role, ()):
            continue
        if (environment and not fnmatchcase(environment, b.environment)) or (namespace and not fnmatchcase(namespace, b.namespace)):
            continue
        out[Grant(environment or b.environment, namespace or b.namespace)] = None
    return list(out)


def in_request(environment: str | None, namespace: str | None, doc_environment: str, doc_namespace: str) -> bool:
    return (not environment or doc_environment in ("*", environment)) and (not namespace or doc_namespace in ("*", namespace))


def _regex(pattern: str) -> str:
    """fnmatch to a PostgreSQL regular expression, anchored."""
    out, i = ["^"], 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            out.append(".*")
        elif c == "?":
            out.append(".")
        elif c == "[" and "]" in pattern[i + 1:]:
            end = pattern.index("]", i + 2 if pattern[i + 1:i + 2] in ("!", "]") else i + 1)
            body = pattern[i + 1:end]
            out.append("[" + ("^" + body[1:] if body.startswith("!") else body) + "]")
            i = end
        else:
            out.append("\\" + c if not c.isalnum() else c)
        i += 1
    return "".join(out) + "$"


def sql(found: list[Grant], params: dict, column: str = "d") -> str:
    """One predicate per grant, so a binding keeps its environment and namespace together."""
    def axis(col: str, value: str, name: str) -> str | None:
        if value == "*":
            return None
        params[name] = value if not any(c in value for c in GLOB) else _regex(value)
        return f"({col} = '*' OR {col} {'~' if any(c in value for c in GLOB) else '='} :{name})"

    parts = []
    for i, g in enumerate(found):
        terms = [x for x in (axis(f"{column}.environment", g.environment, f"ge{i}"), axis(f"{column}.namespace", g.namespace, f"gn{i}")) if x]
        if not terms:
            return "TRUE"  # one unrestricted grant covers every other
        parts.append(" AND ".join(terms))
    return "(" + " OR ".join(f"({p})" for p in parts) + ")" if parts else "FALSE"


def opensearch(found: list[Grant]) -> tuple[dict | None, bool]:
    """(filter, exact). Character classes cannot be expressed, so such an axis is left out and the
    final check decides; `exact` tells the caller that candidates may include unreadable documents."""
    exact = True

    def axis(field: str, value: str) -> dict | None:
        nonlocal exact
        if value == "*":
            return None
        if "[" in value:
            exact = False
            return None
        if any(c in value for c in GLOB):
            return {"bool": {"should": [{"term": {field: "*"}}, {"wildcard": {field: {"value": value}}}], "minimum_should_match": 1}}
        return {"terms": {field: ["*", value]}}

    should = []
    for g in found:
        terms = [x for x in (axis("environment", g.environment), axis("namespace", g.namespace)) if x]
        if not terms:
            return None, exact  # unrestricted
        should.append({"bool": {"filter": terms}})
    return {"bool": {"should": should, "minimum_should_match": 1}} if should else {"bool": {"must_not": [{"match_all": {}}]}}, exact


def pinecone(found: list[Grant]) -> tuple[dict | None, bool]:
    """(filter, exact). Pinecone metadata filters have no patterns."""
    exact = True

    def axis(field: str, value: str) -> dict | None:
        nonlocal exact
        if value == "*":
            return None
        if any(c in value for c in GLOB):
            exact = False
            return None
        return {field: {"$in": ["*", value]}}

    ors = []
    for g in found:
        terms = [x for x in (axis("environment", g.environment), axis("namespace", g.namespace)) if x]
        if not terms:
            return None, exact
        ors.append({"$and": terms} if len(terms) > 1 else terms[0])
    return ({"$or": ors} if len(ors) > 1 else ors[0]) if ors else {"environment": {"$in": []}}, exact
