"""Find what a process said just before it died.

The last error a crashing container wrote is usually the reason, and it is often the only place the
reason exists. Stack frames and shutdown chatter come after or around it, so this looks for the last
line that states a failure, preferring fatal ones. It returns the text verbatim: this reads logs, it
does not interpret them, and a log line is untrusted data wherever it goes next.
"""
from __future__ import annotations

import re

TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})\s+")
FATAL = re.compile(r"(?i)\b(fatal|panic|critical|segmentation fault|out of memory|oomkilled|traceback)\b|\bcould not start\b|\bfailed to start\b")
ERROR = re.compile(r"(?i)\b(error|exception|invalid|missing|not found|no such file|permission denied|denied|refused|unauthorized|forbidden|unable to|cannot|can't|failed|timed? ?out)\b")
NOISE = re.compile(r"(?i)\b(no error|0 errors?|without error|error_count=0|errors?:\s*0)\b|\"level\":\s*\"(info|debug)\"|\blevel=(info|debug)\b")
FRAME = re.compile(r"^\s+(at |File \"|\.\.\.)|^goroutine \d+")


def clean(line: str) -> str:
    return TIMESTAMP.sub("", line).strip()


def last_failure(lines: list[str]) -> str | None:
    """The cause line: the last fatal line, unless a concrete error follows it (a Python traceback
    says "Traceback" first and the exception last), otherwise the last error."""
    cleaned = [clean(ln) for ln in lines]
    useful = [ln for ln in cleaned if ln and not FRAME.match(ln) and not NOISE.search(ln)]
    fatal = next((i for i in range(len(useful) - 1, -1, -1) if FATAL.search(useful[i])), None)
    if fatal is not None:
        after = [ln for ln in useful[fatal + 1:] if ERROR.search(ln)]
        return (after[-1] if after and "traceback" in useful[fatal].lower() else useful[fatal])[:400]
    return next((ln[:400] for ln in reversed(useful) if ERROR.search(ln)), None)
