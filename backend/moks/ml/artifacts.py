"""Model artifacts: written only by MOKS's own training, stored by checksum, validated before every use.

An artifact is canonical JSON. It is parsed with json.loads and nothing else, so loading one runs no code; there
is no upload path, and a pickle is refused by construction. Before a model scores anything MOKS checks:
  - the bytes hash to the SHA-256 the registry recorded (tampering, truncation, the wrong object)
  - the format and library version are ones this build reads
  - the algorithm, feature schema, feature list and sampling step match what the caller is about to feed it
  - the algorithm's state has the expected structure and bounds
Any failure raises ArtifactError, and the caller falls back to the deterministic path.

Storage is PostgreSQL by default (bodies are a few KB to a few hundred KB). `ml.artifactStore: s3` uses the same
S3-compatible client as backups (Cloudflare R2 on mk4), keyed by checksum, and keeps only the URI here.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import MLArtifact
from . import iforest

FORMAT = "moks-ml-artifact"
FORMAT_VERSION = 1
LIBRARY = {"moks_ml": "1.0"}  # the code that reads the state; a major change means retraining
ALGORITHMS = ("seasonal_baseline", "isolation_forest")
MEDIA_TYPE = "application/vnd.moks.ml-artifact+json"


class ArtifactError(Exception):
    pass


def encode(*, algorithm: str, cohort: str, feature_schema: str, features: list[str], step: int, state: Any, params: dict | None = None) -> tuple[bytes, str]:
    if algorithm not in ALGORITHMS:
        raise ArtifactError(f"unknown algorithm {algorithm}")
    doc = {"format": FORMAT, "format_version": FORMAT_VERSION, "library": LIBRARY, "algorithm": algorithm, "cohort": cohort, "feature_schema": feature_schema,
           "features": list(features), "step_seconds": step, "params": params or {}, "created_at": datetime.now(timezone.utc).isoformat(), "state": state}
    body = json.dumps(doc, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return body, hashlib.sha256(body).hexdigest()


def _store():
    from ..backup import Store

    s = get_settings()
    if not s.ml_artifact_bucket:
        raise ArtifactError("ml.artifactStore is s3 but no bucket is configured")
    return Store(s.ml_artifact_bucket, s.ml_artifact_endpoint or None, s.ml_artifact_region or None)


async def save(db: AsyncSession, body: bytes, sha: str) -> MLArtifact:
    s = get_settings()
    if len(body) > s.ml_artifact_max_bytes:
        raise ArtifactError(f"artifact is {len(body)} bytes, above ml_artifact_max_bytes")
    if hashlib.sha256(body).hexdigest() != sha:
        raise ArtifactError("artifact checksum does not match its body")
    existing = await db.get(MLArtifact, sha)
    if existing is not None:
        return existing  # content-addressed: the same bytes are one artifact
    if s.ml_artifact_store == "s3":
        import tempfile
        from pathlib import Path

        key = f"{s.ml_artifact_prefix.rstrip('/')}/{sha}.json"
        with tempfile.NamedTemporaryFile(suffix=".json") as tmp:
            Path(tmp.name).write_bytes(body)
            _store().put(Path(tmp.name), key)
        row = MLArtifact(sha256=sha, media_type=MEDIA_TYPE, body=None, uri=f"s3://{s.ml_artifact_bucket}/{key}", size=len(body))
    else:
        row = MLArtifact(sha256=sha, media_type=MEDIA_TYPE, body=body, uri=None, size=len(body))
    db.add(row)
    await db.flush()
    return row


async def _read(db: AsyncSession, sha: str) -> bytes:
    row = await db.get(MLArtifact, sha)
    if row is None:
        raise ArtifactError("artifact not found")
    if row.media_type != MEDIA_TYPE:
        raise ArtifactError(f"artifact media type {row.media_type} is not one MOKS reads")
    if row.body is not None:
        return bytes(row.body)
    if not row.uri or not row.uri.startswith("s3://"):
        raise ArtifactError("artifact has neither a body nor an s3 URI")
    import tempfile
    from pathlib import Path

    key = row.uri.split("/", 3)[3]
    with tempfile.NamedTemporaryFile(suffix=".json") as tmp:
        _store().download(key, Path(tmp.name))
        return Path(tmp.name).read_bytes()


def decode(body: bytes, sha: str, *, algorithm: str, feature_schema: str, features: list[str], step: int) -> dict[str, Any]:
    """Every check, in order. Pure, so a tampered or mismatched artifact is tested without storage."""
    if len(body) > get_settings().ml_artifact_max_bytes:
        raise ArtifactError("artifact is larger than allowed")
    if hashlib.sha256(body).hexdigest() != sha:
        raise ArtifactError("artifact checksum mismatch: the stored bytes are not the ones the registry recorded")
    try:
        doc = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ArtifactError("artifact is not JSON") from None
    if not isinstance(doc, dict) or doc.get("format") != FORMAT or doc.get("format_version") != FORMAT_VERSION:
        raise ArtifactError("artifact format is not one this build reads")
    lib = (doc.get("library") or {}).get("moks_ml", "")
    if str(lib).split(".")[0] != LIBRARY["moks_ml"].split(".")[0]:
        raise ArtifactError(f"artifact was written by moks_ml {lib}; this build reads {LIBRARY['moks_ml']}")
    if doc.get("algorithm") != algorithm:
        raise ArtifactError(f"artifact is a {doc.get('algorithm')}, not a {algorithm}")
    if doc.get("feature_schema") != feature_schema or list(doc.get("features") or []) != list(features):
        raise ArtifactError("artifact was trained on a different feature schema")
    if doc.get("step_seconds") != step:
        raise ArtifactError(f"artifact was trained at a {doc.get('step_seconds')} s step, not {step} s")
    state = doc.get("state")
    if algorithm == "isolation_forest":
        try:
            iforest.validate(state, len(features))
        except (KeyError, TypeError, ValueError) as exc:
            raise ArtifactError(f"isolation forest state is invalid: {exc}") from None
    elif not isinstance(state, dict) or set(state) != set(features):
        raise ArtifactError("baseline state does not cover the features")
    return doc


async def load(db: AsyncSession, sha: str, **expect: Any) -> dict[str, Any]:
    return decode(await _read(db, sha), sha, **expect)
