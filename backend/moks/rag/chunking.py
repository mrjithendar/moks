"""Chunking tuned for operational content: Markdown splits on headings, YAML on documents,
code on blank-line boundaries; every chunk keeps its line range for citations."""
from __future__ import annotations

import re
from dataclasses import dataclass

TARGET = 1400
OVERLAP_LINES = 3

CODE_EXT = {".py", ".go", ".ts", ".tsx", ".js", ".jsx", ".java", ".kt", ".rb", ".rs", ".sh", ".bash", ".tf", ".hcl", ".sql",
            ".c", ".h", ".cpp", ".cs", ".php", ".scala", ".swift", ".mjs", ".cjs", ".groovy", ".dockerfile"}
MARKDOWN_EXT = {".md", ".markdown", ".mdx", ".rst", ".adoc", ".txt"}
YAML_EXT = {".yaml", ".yml"}
DATA_EXT = {".json", ".toml", ".ini", ".cfg", ".conf", ".properties", ".env.example"}


@dataclass
class ChunkPiece:
    heading: str
    text: str
    start_line: int
    end_line: int


def kind_of(path: str) -> str:
    p = path.lower()
    name = p.rsplit("/", 1)[-1]
    if name in ("dockerfile", "makefile", "jenkinsfile") or name.startswith("dockerfile."):
        return "code"
    ext = "." + name.rsplit(".", 1)[-1] if "." in name else ""
    if ext in MARKDOWN_EXT:
        return "markdown"
    if ext in YAML_EXT:
        return "yaml"
    if ext in CODE_EXT:
        return "code"
    if ext in DATA_EXT:
        return "data"
    return "text"


def title_of(path: str, text: str) -> str:
    m = re.search(r"^#\s+(.+)$", text, re.M)
    return (m.group(1).strip() if m else path.rsplit("/", 1)[-1])[:300]


def chunk(path: str, text: str) -> list[ChunkPiece]:
    k = kind_of(path)
    lines = text.splitlines()
    if not lines:
        return []
    if k == "markdown":
        return _markdown(lines)
    if k == "yaml":
        return _yaml(lines)
    return _windows(lines, 0, "", prefer_blank=k == "code")


def _windows(lines: list[str], offset: int, heading: str, prefer_blank: bool = True) -> list[ChunkPiece]:
    out, start = [], 0
    while start < len(lines):
        size, end = 0, start
        while end < len(lines) and size + len(lines[end]) + 1 <= TARGET:
            size += len(lines[end]) + 1
            end += 1
        if end == start:  # one very long line
            end = start + 1
        elif end < len(lines) and prefer_blank:
            # back off to the last blank line in the second half of the window
            for j in range(end - 1, start + (end - start) // 2, -1):
                if not lines[j].strip():
                    end = j + 1
                    break
        body = "\n".join(lines[start:end]).strip()
        if body:
            out.append(ChunkPiece(heading, body[: TARGET * 2], offset + start + 1, offset + end))
        if end >= len(lines):
            break
        start = max(end - OVERLAP_LINES, start + 1)
    return out


def _markdown(lines: list[str]) -> list[ChunkPiece]:
    sections: list[tuple[str, int, int]] = []
    stack: list[tuple[int, str]] = []
    cur_start, cur_heading = 0, ""
    in_fence = False
    for i, line in enumerate(lines):
        if line.strip().startswith("```"):
            in_fence = not in_fence
        m = None if in_fence else re.match(r"^(#{1,6})\s+(.+?)\s*#*$", line)
        if m:
            if i > cur_start:
                sections.append((cur_heading, cur_start, i))
            level = len(m.group(1))
            stack = [(lv, t) for lv, t in stack if lv < level] + [(level, m.group(2).strip())]
            cur_heading = " > ".join(t for _, t in stack)
            cur_start = i
    sections.append((cur_heading, cur_start, len(lines)))
    out = []
    for heading, s, e in sections:
        out.extend(_windows(lines[s:e], s, heading[:480]))
    return out


def _yaml(lines: list[str]) -> list[ChunkPiece]:
    out, start = [], 0
    bounds = [i for i, l in enumerate(lines) if l.strip() == "---"] + [len(lines)]
    for b in bounds:
        seg = lines[start:b]
        if any(x.strip() for x in seg):
            kind = next((re.sub(r"^kind:\s*", "", x).strip() for x in seg if x.startswith("kind:")), "")
            name = next((x.split(":", 1)[1].strip() for x in seg if re.match(r"^\s{2}name:", x)), "")
            out.extend(_windows(seg, start, f"{kind} {name}".strip()))
        start = b + 1
    return out
