"""In-memory Kubernetes Secrets and AWS Secrets Manager for tests/test_secret_sync.py.

FakeSecretsManager mimics the boto3 client closely enough that the real AwsSecretsConnector runs against it:
ClientRequestToken idempotency, staging labels that live on one version at a time, AWSPREVIOUS following a moved
AWSCURRENT, UpdateSecretVersionStage refusing a RemoveFromVersionId the label is not on, ListSecrets pagination
and filters, recovery-window deletion, and IAM/KMS errors raised as real botocore exceptions.
"""
from __future__ import annotations

import base64
import copy
import itertools
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from botocore.exceptions import ClientError, ReadTimeoutError

from moks.connectors.base import ConnectorError
from moks.secrets.content import Content
from moks.secrets.kube import summary


def client_error(code: str, message: str = "", op: str = "Op", status: int = 400) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}, "ResponseMetadata": {"HTTPStatusCode": status}}, op)


class FakeSecretsKube:
    """One cluster. Secrets are stored as the API returns them: base64 data, resourceVersion, uid."""
    _rv = itertools.count(100)

    def __init__(self, uid: str):
        self.uid = uid
        self.objects: dict[tuple[str, str], dict[str, Any]] = {}
        self.denied_namespaces: set[str] = set()
        self.fail: dict[str, Exception] = {}          # op -> raised once, before acting
        self.after: dict[str, Exception] = {}         # op -> raised once, after acting (an ambiguous outcome)
        self.hooks: dict[str, Callable[[], Any]] = {}  # op -> called before acting (another client racing)
        self.writes: list[tuple[str, str, str]] = []
        self.workload_specs: dict[str, list[dict[str, Any]]] = {}

    def put(self, ns: str, name: str, data: dict[str, bytes], type_: str = "Opaque", **meta: Any) -> dict[str, Any]:
        """Set a Secret directly, as kubectl would."""
        old = self.objects.get((ns, name))
        obj = {"type": type_, "data": {k: base64.b64encode(v).decode() for k, v in data.items()}, "immutable": meta.pop("immutable", False),
               "metadata": {"name": name, "namespace": ns, "uid": old["metadata"]["uid"] if old else str(uuid.uuid4()), "resourceVersion": str(next(self._rv)),
                            "creationTimestamp": "2026-11-01T00:00:00Z", "managedFields": [{"time": datetime.now(timezone.utc).isoformat()}], **meta}}
        self.objects[(ns, name)] = obj
        return obj

    def data(self, ns: str, name: str) -> dict[str, bytes] | None:
        obj = self.objects.get((ns, name))
        return None if obj is None else {k: base64.b64decode(v) for k, v in obj["data"].items()}

    async def _gate(self, op: str, ns: str) -> None:
        if ns in self.denied_namespaces:
            raise ConnectorError("forbidden", f"Kubernetes RBAC denied {op} secrets in {ns} for MOKS's service account")
        if op in self.hooks:
            hook = self.hooks.pop(op)
            hook()
        if op in self.fail:
            raise self.fail.pop(op)

    def _after(self, op: str) -> None:
        if op in self.after:
            raise self.after.pop(op)

    async def aclose(self) -> None:
        pass

    async def cluster_uid(self) -> str:
        return self.uid

    async def list(self, ns: str):
        await self._gate("list", ns)
        return [summary(copy.deepcopy(o)) for (n, _), o in sorted(self.objects.items()) if n == ns], True

    async def get(self, ns: str, name: str):
        await self._gate("get", ns)
        obj = self.objects.get((ns, name))
        if obj is None:
            return None
        return summary(copy.deepcopy(obj)), Content(obj["type"], {k: base64.b64decode(v) for k, v in obj["data"].items()})

    async def create(self, ns, name, content: Content, mapping_id, aws_version):
        await self._gate("create", ns)
        if (ns, name) in self.objects:
            raise ConnectorError("conflict", f"create secret {ns}/{name}: already exists (409 conflict)")
        obj = self.put(ns, name, dict(content.data), content.type, labels={"moks.dev/secret-sync": "true"}, annotations={"moks.dev/secret-sync-mapping": mapping_id})
        self.writes.append(("create", ns, name))
        self._after("create")
        return summary(copy.deepcopy(obj))

    async def replace(self, ns, name, content: Content, resource_version, uid, mapping_id, aws_version):
        await self._gate("replace", ns)
        obj = self.objects.get((ns, name))
        if obj is None or obj["metadata"]["uid"] != uid or obj["metadata"]["resourceVersion"] != resource_version:
            raise ConnectorError("conflict", f"update secret {ns}/{name}: the Secret changed since it was read (409 conflict)")
        if obj["type"] != content.type:
            raise ConnectorError("bad_request", "Kubernetes does not allow changing a Secret's type")
        meta = {k: v for k, v in obj["metadata"].items() if k not in ("name", "namespace", "uid", "resourceVersion", "creationTimestamp", "managedFields")}
        new = self.put(ns, name, dict(content.data), content.type, **meta)
        self.writes.append(("replace", ns, name))
        self._after("replace")
        return summary(copy.deepcopy(new))

    async def delete(self, ns, name, uid, resource_version):
        await self._gate("delete", ns)
        obj = self.objects.get((ns, name))
        if obj is None or obj["metadata"]["uid"] != uid or obj["metadata"]["resourceVersion"] != resource_version:
            raise ConnectorError("conflict", "delete precondition failed (409 conflict)")
        del self.objects[(ns, name)]
        self.writes.append(("delete", ns, name))

    async def workloads(self, ns):
        return self.workload_specs.get(ns, [])


class FakeSTS:
    def __init__(self, account: str):
        self.account = account

    def get_caller_identity(self):
        return {"Account": self.account, "Arn": f"arn:aws:sts::{self.account}:assumed-role/moks-executor/x"}


class FakeSecretsManager:
    """boto3 secretsmanager client semantics for the operations MOKS uses."""

    def __init__(self, account: str = "123456789012", region: str = "eu-west-1"):
        self.account, self.region = account, region
        self.secrets: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        self.deny: dict[str, str] = {}          # op -> error code, every call (IAM or KMS policy)
        self.deny_once: dict[str, ClientError] = {}
        self.timeout_after: set[str] = set()     # op performs, then the response is lost once
        self.hooks: dict[str, Callable[[], Any]] = {}
        self.deny_list_page: int | None = None

    # -- helpers for tests --

    def arn(self, name: str) -> str:
        return f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:{name}-{uuid.uuid5(uuid.NAMESPACE_DNS, name).hex[:6]}"

    def seed(self, name: str, string: str | None = None, binary: bytes | None = None, tags: dict[str, str] | None = None, **extra: Any) -> str:
        vid = str(uuid.uuid4())
        self.secrets[name] = {"arn": self.arn(name), "tags": dict(tags or {}), "kms": extra.get("kms", "alias/aws/secretsmanager"), "deleted": None,
                              "owning_service": extra.get("owning_service"), "rotation": extra.get("rotation", False), "changed": datetime.now(timezone.utc),
                              "versions": {vid: {"string": string, "binary": binary, "stages": {"AWSCURRENT"}, "created": datetime.now(timezone.utc)}}}
        return vid

    def external_put(self, name: str, string: str | None = None, binary: bytes | None = None) -> str:
        """Another client writes a new current version."""
        return self.put_secret_value(SecretId=name, ClientRequestToken=str(uuid.uuid4()), **({"SecretString": string} if string is not None else {"SecretBinary": binary}))["VersionId"]

    def current(self, name: str) -> tuple[str, dict[str, Any]]:
        for vid, v in self.secrets[name]["versions"].items():
            if "AWSCURRENT" in v["stages"]:
                return vid, v
        raise KeyError(name)

    # -- plumbing --

    def _enter(self, op: str) -> None:
        self.calls.append(op)
        if op in self.hooks:
            self.hooks.pop(op)()
        if op in self.deny_once:
            raise self.deny_once.pop(op)
        code = self.deny.get(op)
        if code == "KMS":
            raise client_error("AccessDeniedException", "Access to KMS is not allowed", op)
        if code:
            raise client_error(code, f"User is not authorized to perform secretsmanager:{op}", op)

    def _exit(self, op: str) -> None:
        if op in self.timeout_after:
            self.timeout_after.discard(op)
            raise ReadTimeoutError(endpoint_url=f"https://secretsmanager.{self.region}.amazonaws.com")

    def _find(self, secret_id: str) -> tuple[str, dict[str, Any]]:
        for name, s in self.secrets.items():
            if secret_id in (name, s["arn"]):
                return name, s
        raise client_error("ResourceNotFoundException", "Secrets Manager can't find the specified secret.")

    def _describe(self, name: str, s: dict[str, Any]) -> dict[str, Any]:
        return {"Name": name, "ARN": s["arn"], "KmsKeyId": s["kms"], "Tags": [{"Key": k, "Value": v} for k, v in s["tags"].items()], "LastChangedDate": s["changed"],
                "OwningService": s["owning_service"], "RotationEnabled": s["rotation"], "DeletedDate": s["deleted"],
                "VersionIdsToStages": {vid: sorted(v["stages"]) for vid, v in s["versions"].items() if v["stages"]}}

    # -- the API --

    def list_secrets(self, MaxResults=100, Filters=(), NextToken=None, IncludePlannedDeletion=False):
        self._enter("list_secrets")
        start = int(NextToken or 0)
        page_no = start // MaxResults + 1
        if self.deny_list_page == page_no:
            raise client_error("AccessDeniedException", "not authorized", "ListSecrets")
        prefixes = [v.lower() for f in Filters if f["Key"] == "name" for v in f["Values"]]
        tag_keys = [v for f in Filters if f["Key"] == "tag-key" for v in f["Values"]]
        names = [n for n in sorted(self.secrets) if (not prefixes or any(n.lower().startswith(p) for p in prefixes))
                 and (not tag_keys or any(k in self.secrets[n]["tags"] for k in tag_keys)) and (IncludePlannedDeletion or not self.secrets[n]["deleted"])]
        chunk = names[start:start + MaxResults]
        out = {"SecretList": [{**self._describe(n, self.secrets[n]), "SecretVersionsToStages": self._describe(n, self.secrets[n])["VersionIdsToStages"]} for n in chunk]}
        for d in out["SecretList"]:
            d.pop("VersionIdsToStages")
        if start + MaxResults < len(names):
            out["NextToken"] = str(start + MaxResults)
        return out

    def describe_secret(self, SecretId):
        self._enter("describe_secret")
        return self._describe(*self._find(SecretId))

    def get_secret_value(self, SecretId, VersionId=None, VersionStage=None):
        self._enter("get_secret_value")
        name, s = self._find(SecretId)
        if s["deleted"]:
            raise client_error("InvalidRequestException", "You can't perform this operation on the secret because it was marked for deletion.")
        if VersionId:
            v = s["versions"].get(VersionId)
            if v is None:
                raise client_error("ResourceNotFoundException", "version not found")
            vid = VersionId
        else:
            stage = VersionStage or "AWSCURRENT"
            vid, v = next(((k, x) for k, x in s["versions"].items() if stage in x["stages"]), (None, None))
            if v is None:
                raise client_error("ResourceNotFoundException", f"no version with stage {stage}")
        out = {"ARN": s["arn"], "Name": name, "VersionId": vid, "VersionStages": sorted(v["stages"])}
        if v["binary"] is not None:
            out["SecretBinary"] = v["binary"]
        else:
            out["SecretString"] = v["string"]
        return out

    def create_secret(self, Name, ClientRequestToken, Description="", Tags=(), KmsKeyId=None, SecretString=None, SecretBinary=None):
        self._enter("create_secret")
        if Name in self.secrets:
            raise client_error("ResourceExistsException", f"The operation failed because the secret {Name} already exists.")
        self.secrets[Name] = {"arn": self.arn(Name), "tags": {t["Key"]: t["Value"] for t in Tags}, "kms": KmsKeyId or "alias/aws/secretsmanager", "deleted": None,
                              "owning_service": None, "rotation": False, "changed": datetime.now(timezone.utc),
                              "versions": {ClientRequestToken: {"string": SecretString, "binary": SecretBinary, "stages": {"AWSCURRENT"}, "created": datetime.now(timezone.utc)}}}
        self._exit("create_secret")
        return {"ARN": self.secrets[Name]["arn"], "Name": Name, "VersionId": ClientRequestToken}

    def put_secret_value(self, SecretId, ClientRequestToken, SecretString=None, SecretBinary=None, VersionStages=None):
        self._enter("put_secret_value")
        name, s = self._find(SecretId)
        if s["deleted"]:
            raise client_error("InvalidRequestException", "marked for deletion")
        if ClientRequestToken in s["versions"]:
            v = s["versions"][ClientRequestToken]
            if (v["string"], v["binary"]) != (SecretString, SecretBinary):
                raise client_error("ResourceExistsException", "A resource with the ID you requested already exists.")
            self._exit("put_secret_value")
            return {"ARN": s["arn"], "Name": name, "VersionId": ClientRequestToken, "VersionStages": sorted(v["stages"])}
        stages = set(VersionStages or ["AWSCURRENT"])
        if "AWSCURRENT" in stages:
            for vid, v in s["versions"].items():
                v["stages"].discard("AWSPREVIOUS")
            for vid, v in s["versions"].items():
                if "AWSCURRENT" in v["stages"]:
                    v["stages"].discard("AWSCURRENT")
                    v["stages"].add("AWSPREVIOUS")
        for v in s["versions"].values():
            v["stages"] -= (stages - {"AWSCURRENT"})
        s["versions"][ClientRequestToken] = {"string": SecretString, "binary": SecretBinary, "stages": stages, "created": datetime.now(timezone.utc)}
        s["changed"] = datetime.now(timezone.utc)
        self._exit("put_secret_value")
        return {"ARN": s["arn"], "Name": name, "VersionId": ClientRequestToken, "VersionStages": sorted(stages)}

    def update_secret_version_stage(self, SecretId, VersionStage, MoveToVersionId=None, RemoveFromVersionId=None):
        self._enter("update_secret_version_stage")
        name, s = self._find(SecretId)
        holder = next((vid for vid, v in s["versions"].items() if VersionStage in v["stages"]), None)
        if RemoveFromVersionId and holder != RemoveFromVersionId:
            raise client_error("InvalidParameterException", f"The staging label {VersionStage} is not attached to version {RemoveFromVersionId}.")
        if MoveToVersionId and holder and holder != MoveToVersionId and not RemoveFromVersionId:
            raise client_error("InvalidParameterException", f"The staging label {VersionStage} is attached to another version; specify RemoveFromVersionId.")
        if MoveToVersionId and MoveToVersionId not in s["versions"]:
            raise client_error("ResourceNotFoundException", "version not found")
        if holder and (MoveToVersionId or RemoveFromVersionId):
            s["versions"][holder]["stages"].discard(VersionStage)
        if MoveToVersionId:
            s["versions"][MoveToVersionId]["stages"].add(VersionStage)
            if VersionStage == "AWSCURRENT" and holder and holder != MoveToVersionId:
                for v in s["versions"].values():
                    v["stages"].discard("AWSPREVIOUS")
                s["versions"][holder]["stages"].add("AWSPREVIOUS")
        s["changed"] = datetime.now(timezone.utc)
        self._exit("update_secret_version_stage")
        return {"ARN": s["arn"], "Name": name}

    def list_secret_version_ids(self, SecretId, MaxResults=100, IncludeDeprecated=False, NextToken=None):
        self._enter("list_secret_version_ids")
        _, s = self._find(SecretId)
        return {"Versions": [{"VersionId": vid, "VersionStages": sorted(v["stages"]), "CreatedDate": v["created"]} for vid, v in s["versions"].items()
                             if v["stages"] or IncludeDeprecated]}

    def delete_secret(self, SecretId, RecoveryWindowInDays=30, ForceDeleteWithoutRecovery=False):
        self._enter("delete_secret")
        assert not ForceDeleteWithoutRecovery, "MOKS must never force-delete"
        name, s = self._find(SecretId)
        s["deleted"] = datetime.now(timezone.utc) + timedelta(days=RecoveryWindowInDays)
        s["recovery_days"] = RecoveryWindowInDays
        return {"ARN": s["arn"], "Name": name, "DeletionDate": s["deleted"]}
