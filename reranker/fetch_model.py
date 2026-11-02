"""Downloads the pinned model into a directory and verifies every file. Runs at image build, never at
runtime: the service loads from local files only, so a running pod needs no network and cannot drift."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def verify(lock: dict, target: Path) -> None:
    for name, expected in lock["files"].items():
        got = digest(target / name)
        if got != expected:
            raise SystemExit(f"{name}: expected {expected}, got {got}")


def main(lock_path: str, target: str) -> None:
    from huggingface_hub import snapshot_download

    lock = json.loads(Path(lock_path).read_text())
    snapshot_download(lock["model_id"], revision=lock["revision"], allow_patterns=list(lock["files"]), local_dir=target)
    verify(lock, Path(target))
    print(f"{lock['model_id']}@{lock['revision']} verified")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
