"""Secret exclusion for ingestion: files that look like credentials are never indexed, and the
rest is redacted before chunking. Excluded documents are recorded (status=excluded) with a reason
so operators can see what was skipped, but their content is not stored."""
from __future__ import annotations

import re

from ..connectors.git import SECRET_PATHS
from ..redaction import redact

PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
K8S_SECRET = re.compile(r"(?m)^kind:\s*Secret\s*$")
BINARY = re.compile(r"[\x00-\x08\x0e-\x1f]")


def exclusion_reason(path: str, text: str) -> str | None:
    if SECRET_PATHS.search(path):
        return "path matches secret-file patterns"
    if BINARY.search(text[:4000]):
        return "binary content"
    if PRIVATE_KEY.search(text):
        return "contains a private key"
    if K8S_SECRET.search(text) and re.search(r"(?m)^(data|stringData):", text):
        return "Kubernetes Secret manifest with data"
    redacted = redact(text)
    if text and redacted.count("[REDACTED]") > 20:
        return "too many credential-like values"
    return None


def clean(text: str) -> str:
    return redact(text)
