"""Secret redaction for anything that leaves a connector: evidence storage, LLM prompts,
audit details and API responses.

Pattern-based redaction is a safety net, not a guarantee: it catches common credential
shapes. Connectors additionally never read Kubernetes Secrets or environment values that
reference them (see moks.connectors.kubernetes).
"""
from __future__ import annotations

import re
from typing import Any

MASK = "[REDACTED]"

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # PEM private keys
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), MASK),
    # Authorization headers / bearer tokens
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + MASK),
    # JWTs
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), MASK),
    # AWS access key ids and secret-looking assignments
    (re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b"), MASK),
    (re.compile(r"(?i)(aws_secret_access_key|aws_session_token)(\s*[=:]\s*)\S+"), r"\1\2" + MASK),
    # Common provider keys
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), MASK),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"), MASK),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), MASK),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"), MASK),
    # Credentials embedded in URLs: scheme://user:pass@host
    (re.compile(r"([a-z][a-z0-9+.-]*://[^/\s:@]+:)[^@\s/]+@", re.I), r"\1" + MASK + "@"),
    # key=value / "key": "value" pairs with sensitive key names
    (
        re.compile(
            r"""(?ix)
            (["']?(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|client[_-]?secret|private[_-]?key|credentials?|session[_-]?id|cookie)["']?)
            (\s*[:=]\s*)
            ("[^"]*"|'[^']*'|[^\s,;&}]+)
            """
        ),
        r"\1\2" + MASK,
    ),
]

_SENSITIVE_KEYS = re.compile(
    r"(?i)^(password|passwd|secret|token|api[_-]?key|apikey|authorization|cookie|set-cookie|"
    r"client[_-]?secret|private[_-]?key|access[_-]?key|session[_-]?token|credentials?)$"
)


def redact(text: str) -> str:
    if not text:
        return text
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


def redact_obj(value: Any, _depth: int = 0) -> Any:
    """Recursively redact strings and values under sensitive keys."""
    if _depth > 20:
        return value
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and _SENSITIVE_KEYS.match(k) and v not in (None, "", [], {}):
                out[k] = MASK
            else:
                out[k] = redact_obj(v, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_obj(v, _depth + 1) for v in value]
    return value
