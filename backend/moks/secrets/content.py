"""Secret content, the payload formats it is stored in on AWS, and the keyed digests it is compared by.

Content is a Kubernetes view of a secret: a type and raw bytes per key. Every format converts to and from it, so
both sides are compared in one shape. A key that is absent is not the same as a key whose value is empty: both
survive every format that can carry them, and a format that cannot is refused rather than approximated.

Formats on AWS:
  moks-k8s-v1  {"format": "moks.k8s-secret/v1", "type": ..., "data": {key: base64}} in SecretString. Lossless:
               any key, any bytes, empty values and the Secret type.
  json         {aws_key: "text"} in SecretString. Values must be UTF-8 text. key_mapping selects and renames keys;
               on export, AWS keys outside the mapping are kept as they are.
  text         The whole SecretString is one Kubernetes key (key_mapping {"key": name}). Must be UTF-8.
  binary       The whole SecretBinary is one Kubernetes key.

Digests. A per-key digest is HMAC-SHA256 under the executor's digest key, over the key name and value, so a stored
digest of a short password cannot be brute-forced without that key. A content digest is SHA-256 over the type and
the sorted per-key digests: the API can work out the digest of any combination of keys (an edited conflict
resolution) without the key or a value, and the executor checks the destination against it after writing.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import Any, Mapping

ENVELOPE = "moks.k8s-secret/v1"
FORMATS = ("moks-k8s-v1", "json", "text", "binary")
AWS_MAX_BYTES = 65536
K8S_MAX_BYTES = 1_000_000  # the API server's limit is 1 MiB for the whole object; leave room for metadata
SUPPORTED_TYPES = ("Opaque", "kubernetes.io/tls", "kubernetes.io/dockerconfigjson", "kubernetes.io/dockercfg",
                   "kubernetes.io/basic-auth", "kubernetes.io/ssh-auth")


class FormatError(Exception):
    """The content cannot be carried by this mapping's format without loss. keys: the keys concerned, never values."""

    def __init__(self, message: str, keys: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.keys = keys or []


@dataclass(frozen=True)
class Content:
    type: str
    data: Mapping[str, bytes] = field(default_factory=dict)

    def __repr__(self) -> str:  # values never reach a log line or a traceback through repr
        return f"Content(type={self.type!r}, keys={sorted(self.data)!r})"

    __str__ = __repr__

    def size(self) -> int:
        return sum(len(k) + len(v) for k, v in self.data.items())


@dataclass(frozen=True)
class AwsValue:
    """What GetSecretValue returned. Exactly one of string and binary is set."""
    string: str | None = None
    binary: bytes | None = None
    version_id: str | None = None
    stages: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return f"AwsValue(kind={'binary' if self.binary is not None else 'string'}, version_id={self.version_id!r})"

    __str__ = __repr__


def _text(value: bytes, key: str) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        raise FormatError(f"key {key!r} holds binary data, which this format cannot carry; use moks-k8s-v1", [key]) from None


def selected(key_mapping: dict[str, Any] | None) -> dict[str, str] | None:
    """{aws_key: k8s_key} for a json mapping that selects keys, or None for all keys under their own names."""
    keys = (key_mapping or {}).get("keys")
    return dict(keys) if keys else None


def single_key(key_mapping: dict[str, Any] | None) -> str:
    key = (key_mapping or {}).get("key")
    if not key:
        raise FormatError("text and binary formats need key_mapping.key: the Kubernetes key that holds the value")
    return key


def to_aws(content: Content, fmt: str, key_mapping: dict[str, Any] | None, current: AwsValue | None = None) -> AwsValue:
    """The AWS payload for content. current: the destination's present value, kept for json keys outside the mapping."""
    if fmt == "moks-k8s-v1":
        body = {"format": ENVELOPE, "type": content.type, "data": {k: base64.b64encode(v).decode() for k, v in sorted(content.data.items())}}
        out = AwsValue(string=json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    elif fmt == "json":
        chosen = selected(key_mapping)
        if chosen is None:
            obj = {k: _text(v, k) for k, v in sorted(content.data.items())}
        else:
            inverse = {k8s: aws for aws, k8s in chosen.items()}
            outside = [k for k in content.data if k not in inverse]
            if outside:
                raise FormatError("the Secret has keys this mapping does not carry; add them to key_mapping.keys", sorted(outside))
            obj = {}
            if current is not None and current.string:
                try:
                    kept = json.loads(current.string)
                except ValueError:
                    kept = None
                if not isinstance(kept, dict):
                    raise FormatError("the AWS secret is not a JSON object, so its other keys cannot be kept")
                obj = {k: v for k, v in kept.items() if k not in chosen}
            for k8s, v in content.data.items():
                obj[inverse[k8s]] = _text(v, k8s)
        out = AwsValue(string=json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    elif fmt == "text":
        key = single_key(key_mapping)
        extra = sorted(k for k in content.data if k != key)
        if extra:
            raise FormatError(f"text format carries only key {key!r}", extra)
        if key not in content.data:
            raise FormatError(f"the Secret has no key {key!r} to store as text", [key])
        out = AwsValue(string=_text(content.data[key], key))
    elif fmt == "binary":
        key = single_key(key_mapping)
        extra = sorted(k for k in content.data if k != key)
        if extra:
            raise FormatError(f"binary format carries only key {key!r}", extra)
        if key not in content.data:
            raise FormatError(f"the Secret has no key {key!r} to store as binary", [key])
        out = AwsValue(binary=bytes(content.data[key]))
    else:
        raise FormatError(f"unknown payload format {fmt!r}")
    size = len(out.string.encode()) if out.string is not None else len(out.binary or b"")
    if size > AWS_MAX_BYTES:
        raise FormatError(f"the encoded value is {size} bytes; AWS Secrets Manager stores at most {AWS_MAX_BYTES}")
    return out


def from_aws(value: AwsValue, fmt: str, key_mapping: dict[str, Any] | None, k8s_type: str) -> Content:
    """Content of an AWS value under this mapping. Raises FormatError for anything that would not round-trip."""
    if fmt == "binary":
        if value.binary is None:
            raise FormatError("the AWS secret holds a string, but the mapping expects SecretBinary")
        return Content(k8s_type, {single_key(key_mapping): bytes(value.binary)})
    if value.string is None:
        raise FormatError("the AWS secret holds SecretBinary; use the binary format to import it")
    if fmt == "text":
        return Content(k8s_type, {single_key(key_mapping): value.string.encode("utf-8")})
    try:
        obj = json.loads(value.string)
    except ValueError:
        raise FormatError("the AWS secret is not JSON; import it with the text format and a key name") from None
    if not isinstance(obj, dict):
        raise FormatError("the AWS secret is JSON but not an object; import it with the text format")
    if fmt == "moks-k8s-v1":
        if obj.get("format") != ENVELOPE or not isinstance(obj.get("data"), dict) or not isinstance(obj.get("type"), str):
            raise FormatError(f"the AWS secret is not a {ENVELOPE} envelope; choose the json or text format to import it")
        data: dict[str, bytes] = {}
        bad = []
        for k, v in obj["data"].items():
            try:
                data[k] = base64.b64decode(v, validate=True) if isinstance(v, str) else None  # type: ignore[assignment]
            except (binascii.Error, ValueError):
                data[k] = None  # type: ignore[assignment]
            if data[k] is None:
                bad.append(k)
        if bad:
            raise FormatError("the envelope has keys that are not valid base64", sorted(bad))
        return Content(obj["type"], data)
    if fmt == "json":
        chosen = selected(key_mapping)
        pairs = obj.items() if chosen is None else ((aws, obj[aws]) for aws in chosen if aws in obj)
        data, bad = {}, []
        for aws_key, v in pairs:
            if not isinstance(v, str):
                bad.append(aws_key)
                continue
            data[aws_key if chosen is None else chosen[aws_key]] = v.encode("utf-8")
        if bad:
            raise FormatError("these JSON values are not strings, so they would not round-trip; map other keys or use the text format", sorted(bad))
        return Content(k8s_type, data)
    raise FormatError(f"unknown payload format {fmt!r}")


def from_kubernetes(obj_type: str | None, data: Mapping[str, str] | None) -> Content:
    """Content of a Secret as the API returns it (base64 data)."""
    return Content(obj_type or "Opaque", {k: base64.b64decode(v or "") for k, v in (data or {}).items()})


def to_kubernetes(content: Content) -> dict[str, str]:
    if content.size() > K8S_MAX_BYTES:
        raise FormatError("the content is larger than a Kubernetes Secret can hold")
    return {k: base64.b64encode(v).decode() for k, v in content.data.items()}


# ---- Digests ----------------------------------------------------------------------------------------------------


def key_id(digest_key: bytes) -> str:
    """Names the digest key, so a rotated key invalidates old baselines instead of making every secret look changed."""
    return hashlib.sha256(b"moks.secret-digest-key\0" + digest_key).hexdigest()[:12]


def key_digest(digest_key: bytes, name: str, value: bytes) -> str:
    return hmac.new(digest_key, b"moks.secret.v1\0" + name.encode("utf-8") + b"\0" + value, hashlib.sha256).hexdigest()


def summarize(digest_key: bytes, content: Content) -> dict[str, dict[str, Any]]:
    """Per key: keyed digest, whether the value is empty, and whether it is binary. Safe to store and show."""
    out = {}
    for k, v in content.data.items():
        try:
            v.decode("utf-8")
            binary = False
        except UnicodeDecodeError:
            binary = True
        out[k] = {"d": key_digest(digest_key, k, v), "empty": len(v) == 0, "binary": binary}
    return out


def content_digest(type_: str, keys: Mapping[str, Mapping[str, Any]]) -> str:
    material = json.dumps({"t": type_, "k": sorted((k, v["d"]) for k, v in keys.items())}, separators=(",", ":"))
    return hashlib.sha256(material.encode()).hexdigest()


def fingerprint(entry: Mapping[str, Any] | None) -> str | None:
    """The short form shown in a review: enough to see that two values differ, nothing about either."""
    return entry["d"][:8] if entry else None


def diff(source: Mapping[str, Mapping[str, Any]], dest: Mapping[str, Mapping[str, Any]] | None) -> dict[str, Any]:
    """What writing source over dest changes, by key name. Values appear only as masked fingerprints."""
    dest = dest or {}
    added = sorted(k for k in source if k not in dest)
    removed = sorted(k for k in dest if k not in source)
    changed = [{"key": k, "from": {"fp": fingerprint(dest[k]), "empty": dest[k]["empty"], "binary": dest[k]["binary"]},
                "to": {"fp": fingerprint(source[k]), "empty": source[k]["empty"], "binary": source[k]["binary"]}}
               for k in sorted(source) if k in dest and source[k]["d"] != dest[k]["d"]]
    return {"added": [{"key": k, "to": {"fp": fingerprint(source[k]), "empty": source[k]["empty"], "binary": source[k]["binary"]}} for k in added],
            "removed": [{"key": k, "from": {"fp": fingerprint(dest[k]), "empty": dest[k]["empty"], "binary": dest[k]["binary"]}} for k in removed],
            "changed": changed, "unchanged": sorted(k for k in source if k in dest and source[k]["d"] == dest[k]["d"])}


def merge(kube: Mapping[str, Mapping[str, Any]], aws: Mapping[str, Mapping[str, Any]], choices: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    """Per-key resolution: each key in either side takes its value from the side chosen for it, or is omitted.
    A key with no choice is refused: an edited resolution must say what happens to every key."""
    out: dict[str, dict[str, Any]] = {}
    for k in sorted(set(kube) | set(aws)):
        pick = choices.get(k)
        if pick is None:
            raise FormatError(f"choose kubernetes, aws or omit for key {k!r}", [k])
        if pick == "omit":
            continue
        side = kube if pick == "kubernetes" else aws if pick == "aws" else None
        if side is None:
            raise FormatError(f"unknown choice {pick!r} for key {k!r}", [k])
        if k not in side:
            raise FormatError(f"key {k!r} does not exist in {pick}", [k])
        out[k] = dict(side[k])
    return out


def assemble(kube: Content | None, aws: Content | None, choices: Mapping[str, str], type_: str) -> Content:
    """The executor's side of merge(): the same selection over real values."""
    data: dict[str, bytes] = {}
    for k in sorted(set((kube.data if kube else {})) | set((aws.data if aws else {}))):
        pick = choices.get(k)
        if pick == "omit":
            continue
        src = kube if pick == "kubernetes" else aws if pick == "aws" else None
        if src is None or k not in src.data:
            raise FormatError(f"no value for key {k!r} on the chosen side", [k])
        data[k] = src.data[k]
    return Content(type_, data)
