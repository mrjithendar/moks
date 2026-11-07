"""AWS Secrets Manager connector (type aws_secrets).

Boundaries are enforced here, independent of IAM: one account (checked with sts:GetCallerIdentity on every client
build), one region, secret names under config.allowed_prefixes, and every tag in config.required_tags. A secret
outside them is invisible to discovery and refused for any read of its value or any write.

Credentials: on EKS, the default chain (Pod Identity or IRSA on moks-executor). On Minikube, config.credentials holds
credential references, {"access_key_id": {"type": "env", "name": "MOKS_CRED_..."}, "secret_access_key": {...}},
resolved in the executor from its own Secret (secretsSync.executorSecret), so no other pod holds the keys.
config.role_arn is assumed for another account; that role must belong to config.account_id.

Only the executor process uses a client. The API builds this class to validate configuration and never calls AWS.

Writes. PutSecretValue never moves AWSCURRENT directly: the new version is staged as MOKSPENDING, then
UpdateSecretVersionStage moves AWSCURRENT to it with RemoveFromVersionId set to the version the plan was approved
against. Secrets Manager refuses that move when AWSCURRENT is no longer there, so the swap is conditional on the
reviewed version. ClientRequestToken is the plan item's token: repeating a write after an ambiguous failure
returns the version that already exists instead of creating another. Encryption settings and tags of an existing
secret are never changed.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any

from ..config import get_settings
from ..connectors.base import ConnectorError, describe_credential_ref, resolve_credential
from ..redaction import redact
from .content import AwsValue

NAME = re.compile(r"^[A-Za-z0-9/_+=.@-]{1,512}$")
REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
ACCOUNT = re.compile(r"^\d{12}$")
PENDING = "MOKSPENDING"
CURRENT, PREVIOUS = "AWSCURRENT", "AWSPREVIOUS"
UNCERTAIN = {"timeout", "unavailable"}  # the request may have reached AWS


@dataclass(frozen=True)
class SecretMeta:
    name: str
    arn: str
    version_id: str | None
    last_changed: str | None
    kms_key_id: str | None
    tags: dict[str, str]
    owning_service: str | None
    rotation_enabled: bool
    deleted_date: str | None
    description: str

    def out(self) -> dict[str, Any]:
        return {"name": self.name, "arn": self.arn, "version_id": self.version_id, "last_changed": self.last_changed, "kms_key_id": self.kms_key_id,
                "tags": self.tags, "owning_service": self.owning_service, "rotation_enabled": self.rotation_enabled, "deleted_date": self.deleted_date}


def _iso(v: Any) -> str | None:
    return v.isoformat() if hasattr(v, "isoformat") else (str(v) if v else None)


def _meta(d: dict[str, Any]) -> SecretMeta:
    current = None
    for vid, stages in (d.get("VersionIdsToStages") or d.get("SecretVersionsToStages") or {}).items():  # Describe / List spellings
        if CURRENT in stages:
            current = vid
    return SecretMeta(name=d["Name"], arn=d["ARN"], version_id=current, last_changed=_iso(d.get("LastChangedDate")), kms_key_id=d.get("KmsKeyId"),
                      tags={t["Key"]: t["Value"] for t in d.get("Tags") or []}, owning_service=d.get("OwningService"),
                      rotation_enabled=bool(d.get("RotationEnabled")), deleted_date=_iso(d.get("DeletedDate")), description=d.get("Description") or "")


class AwsSecretsConnector:
    type = "aws_secrets"

    def __init__(self, config: dict[str, Any], credential_ref: dict[str, Any] | None = None, client: Any = None, sts: Any = None):
        self.account_id = str(config.get("account_id") or "")
        if not ACCOUNT.match(self.account_id):
            raise ConnectorError("config", "aws_secrets needs config.account_id (12 digits)")
        self.region = config.get("region") or ""
        if not REGION.match(self.region):
            raise ConnectorError("config", f"invalid AWS region {self.region!r}")
        self.prefixes = [str(p) for p in config.get("allowed_prefixes") or []]
        if not self.prefixes or not all(NAME.match(p) for p in self.prefixes):
            raise ConnectorError("config", "aws_secrets needs config.allowed_prefixes: name prefixes such as moks/ (letters, digits and /_+=.@-)")
        self.required_tags = {str(k): str(v) for k, v in (config.get("required_tags") or {}).items()}
        # Empty means every namespace in secretsSync.namespaces; a list narrows that, and never widens it (the executor has
        # Roles only there).
        self.namespaces = [str(n) for n in config.get("kubernetes_namespaces") or []] or get_settings().secrets_namespace_list
        if not self.namespaces:
            raise ConnectorError("config", "aws_secrets needs config.kubernetes_namespaces, or secretsSync.namespaces in the chart")
        self.role_arn = config.get("role_arn") or None
        if self.role_arn and (self.role_arn.count(":") < 5 or self.role_arn.split(":")[4] != self.account_id):
            raise ConnectorError("config", "config.role_arn must be a role in config.account_id")
        self.external_id = config.get("external_id") or None
        self.kms_key_id = config.get("kms_key_id") or None
        self.allow_delete = bool(config.get("allow_delete"))
        self.recovery_window_days = int(config.get("recovery_window_days", 30))
        if not 7 <= self.recovery_window_days <= 30:
            raise ConnectorError("config", "recovery_window_days must be between 7 and 30")
        self.name_template = config.get("name_template") or "moks/{environment}/{cluster}/{namespace}/{name}"
        self.credentials = config.get("credentials") or None
        if self.credentials is not None:
            if set(self.credentials) != {"access_key_id", "secret_access_key"} or not all(isinstance(r, dict) and r.get("type") in ("env", "file") for r in self.credentials.values()):
                raise ConnectorError("config", "config.credentials needs access_key_id and secret_access_key credential references (env or file); values are never accepted")
        self._client = client
        self._sts = sts
        self._verified = False
        self._lock = asyncio.Lock()

    # ---- boundaries (pure) ---------------------------------------------------------------------------------

    def describe_scope(self) -> dict[str, Any]:
        return {"account_id": self.account_id, "region": self.region, "allowed_prefixes": self.prefixes, "required_tags": self.required_tags,
                "credentials": {k: describe_credential_ref(v) for k, v in self.credentials.items()} if self.credentials else "default chain (Pod Identity, IRSA)",
                "kubernetes_namespaces": self.namespaces, "role_arn": self.role_arn, "kms_key_id": self.kms_key_id, "allow_delete": self.allow_delete,
                "recovery_window_days": self.recovery_window_days, "name_template": self.name_template}

    def name_allowed(self, name: str) -> bool:
        return bool(NAME.match(name)) and any(name.startswith(p) for p in self.prefixes)

    def tags_allowed(self, tags: dict[str, str]) -> bool:
        return all(tags.get(k) == v for k, v in self.required_tags.items())

    def namespace_allowed(self, namespace: str) -> bool:
        chart = get_settings().secrets_namespace_list
        return any(fnmatchcase(namespace, p) for p in self.namespaces) and (not chart or namespace in chart)

    def arn_for(self, name: str) -> str:
        return f"arn:aws:secretsmanager:{self.region}:{self.account_id}:secret:{name}"

    def owns_arn(self, arn: str) -> bool:
        return arn.startswith(f"arn:aws:secretsmanager:{self.region}:{self.account_id}:secret:")

    def check(self, meta: SecretMeta) -> None:
        if not self.owns_arn(meta.arn):
            raise ConnectorError("forbidden", f"{meta.name} is not in account {self.account_id} region {self.region}")
        if not self.name_allowed(meta.name):
            raise ConnectorError("forbidden", f"{meta.name} is outside the allowed prefixes {', '.join(self.prefixes)}")
        if not self.tags_allowed(meta.tags):
            raise ConnectorError("forbidden", f"{meta.name} lacks the required tags {self.required_tags}")

    # ---- client ---------------------------------------------------------------------------------------------

    def _require_executor(self) -> None:
        if get_settings().process_role != "executor":
            raise ConnectorError("forbidden", "AWS Secrets Manager is reached only from the executor process")

    def _build(self) -> tuple[Any, Any]:
        import boto3
        from botocore.config import Config

        cfg = Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 3, "mode": "standard"})
        if self.credentials:
            base = boto3.session.Session(aws_access_key_id=resolve_credential(self.credentials["access_key_id"]),
                                         aws_secret_access_key=resolve_credential(self.credentials["secret_access_key"]), region_name=self.region)
        else:
            base = boto3.session.Session(region_name=self.region)
        session = base
        if self.role_arn:
            extra = {"ExternalId": self.external_id} if self.external_id else {}
            creds = base.client("sts", config=cfg).assume_role(RoleArn=self.role_arn, RoleSessionName="moks-secrets", DurationSeconds=3600, **extra)["Credentials"]
            session = boto3.session.Session(aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretAccessKey"],
                                            aws_session_token=creds["SessionToken"], region_name=self.region)
        return session.client("secretsmanager", region_name=self.region, config=cfg), session.client("sts", region_name=self.region, config=cfg)

    async def _ready(self) -> Any:
        self._require_executor()
        async with self._lock:
            if self._client is None:
                try:
                    self._client, self._sts = await asyncio.to_thread(self._build)
                except ConnectorError:
                    raise
                except Exception as exc:
                    raise self._error(exc, "assume the configured role") from None
            if not self._verified:
                if self._sts is not None:
                    ident = await self._raw(self._sts, "get_caller_identity", "check the AWS identity")
                    if str(ident.get("Account")) != self.account_id:
                        raise ConnectorError("forbidden", f"MOKS's AWS identity is in account {ident.get('Account')}, not {self.account_id}")
                self._verified = True
        return self._client

    def _error(self, exc: Exception, what: str) -> ConnectorError:
        from botocore.exceptions import BotoCoreError, ClientError, ConnectTimeoutError, EndpointConnectionError, NoCredentialsError, ReadTimeoutError

        if isinstance(exc, NoCredentialsError):
            return ConnectorError("auth", "no AWS credentials are available to the executor")
        if isinstance(exc, (ReadTimeoutError, ConnectTimeoutError)):
            return ConnectorError("timeout", f"AWS Secrets Manager timed out during {what}", retryable=True)
        if isinstance(exc, EndpointConnectionError):
            return ConnectorError("unavailable", f"cannot reach AWS Secrets Manager during {what}", retryable=True)
        if isinstance(exc, ClientError):
            err = exc.response.get("Error", {})
            code, message = err.get("Code", "Error"), redact(str(err.get("Message", "")))[:300]
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 0
            if code in ("DecryptionFailure", "EncryptionFailure") or (code == "AccessDeniedException" and "kms" in message.lower()):
                return ConnectorError("kms", f"KMS refused {what}: {code}. The key policy or kms:Decrypt/kms:GenerateDataKey for MOKS's role is missing")
            if code in ("AccessDeniedException", "AccessDenied", "UnauthorizedOperation"):
                return ConnectorError("forbidden", f"IAM denied {what} ({code})")
            if code in ("ExpiredTokenException", "UnrecognizedClientException", "InvalidClientTokenId"):
                return ConnectorError("auth", f"AWS rejected MOKS's credentials during {what} ({code})")
            if code == "ResourceNotFoundException":
                return ConnectorError("not_found", f"{what}: the secret does not exist")
            if code == "ResourceExistsException":
                return ConnectorError("conflict", f"{what}: {message or 'already exists'}")
            if code == "InvalidParameterException":
                return ConnectorError("conflict", f"{what}: {message or 'the version or label changed'}")
            if code == "InvalidRequestException":
                return ConnectorError("bad_request", f"{what}: {message or 'the request is not valid for this secret now'}")
            if code in ("ThrottlingException", "TooManyRequestsException", "InternalServiceError") or status >= 500:
                return ConnectorError("unavailable", f"AWS Secrets Manager error during {what} ({code})", retryable=True)
            return ConnectorError("bad_request", f"AWS rejected {what} ({code})")
        if isinstance(exc, BotoCoreError):
            return ConnectorError("unavailable", f"{what} failed ({type(exc).__name__})", retryable=True)
        if isinstance(exc, asyncio.TimeoutError):
            return ConnectorError("timeout", f"AWS Secrets Manager timed out during {what}", retryable=True)
        return ConnectorError("unavailable", f"{what} failed ({type(exc).__name__})")

    async def _raw(self, client: Any, op: str, what: str, **kwargs: Any) -> dict:
        try:
            return await asyncio.wait_for(asyncio.to_thread(lambda: getattr(client, op)(**kwargs)), 45)
        except ConnectorError:
            raise
        except Exception as exc:
            raise self._error(exc, what) from None

    async def _call(self, op: str, what: str, **kwargs: Any) -> dict:
        return await self._raw(await self._ready(), op, what, **kwargs)

    async def test(self) -> str:
        if get_settings().process_role != "executor":
            return f"configuration valid for account {self.account_id} in {self.region}; credentials are checked by the executor on discovery"
        await self._ready()
        return f"AWS identity verified in account {self.account_id}"

    async def aclose(self) -> None:
        self._client = None

    # ---- reads ----------------------------------------------------------------------------------------------

    async def list_secrets(self, max_pages: int) -> tuple[list[SecretMeta], bool, str | None]:
        """Metadata of in-bounds secrets, page by page. Returns (secrets, complete, error): a failure after some
        pages keeps what was read and says the list is partial."""
        out: list[SecretMeta] = []
        filters = [{"Key": "name", "Values": self.prefixes}]
        if self.required_tags:
            filters.append({"Key": "tag-key", "Values": list(self.required_tags)})
        token = None
        for _ in range(max_pages):
            kwargs: dict[str, Any] = {"MaxResults": 100, "Filters": filters, "IncludePlannedDeletion": True}
            if token:
                kwargs["NextToken"] = token
            try:
                page = await self._call("list_secrets", "list secrets", **kwargs)
            except ConnectorError as exc:
                if not out:
                    raise
                return out, False, exc.message
            for d in page.get("SecretList") or []:
                meta = _meta(d)
                # the name filter is a case-insensitive prefix match and the tag filter checks keys only
                if self.owns_arn(meta.arn) and self.name_allowed(meta.name) and self.tags_allowed(meta.tags):
                    out.append(meta)
            token = page.get("NextToken")
            if not token:
                return out, True, None
        return out, False, f"stopped after {max_pages} pages; narrow allowed_prefixes or raise MOKS_SECRETS_DISCOVERY_MAX_PAGES"

    async def describe(self, secret_id: str) -> SecretMeta | None:
        try:
            meta = _meta(await self._call("describe_secret", f"describe {secret_id}", SecretId=secret_id))
        except ConnectorError as exc:
            if exc.kind == "not_found":
                return None
            raise
        self.check(meta)
        return meta

    async def get_value(self, meta: SecretMeta, version_id: str | None = None, stage: str | None = None) -> AwsValue:
        self.check(meta)
        kwargs: dict[str, Any] = {"SecretId": meta.arn}
        if version_id:
            kwargs["VersionId"] = version_id
        elif stage:
            kwargs["VersionStage"] = stage
        r = await self._call("get_secret_value", f"read {meta.name}", **kwargs)
        return AwsValue(string=r.get("SecretString"), binary=r.get("SecretBinary"), version_id=r.get("VersionId"), stages=tuple(r.get("VersionStages") or ()))

    async def versions(self, meta: SecretMeta) -> list[dict[str, Any]]:
        self.check(meta)
        out, token = [], None
        for _ in range(10):
            kwargs: dict[str, Any] = {"SecretId": meta.arn, "MaxResults": 100, "IncludeDeprecated": True}
            if token:
                kwargs["NextToken"] = token
            page = await self._call("list_secret_version_ids", f"list versions of {meta.name}", **kwargs)
            out += [{"version_id": v["VersionId"], "stages": list(v.get("VersionStages") or []), "created": _iso(v.get("CreatedDate"))} for v in page.get("Versions") or []]
            token = page.get("NextToken")
            if not token:
                break
        return out

    # ---- writes ---------------------------------------------------------------------------------------------

    @staticmethod
    def _payload(value: AwsValue) -> dict[str, Any]:
        return {"SecretBinary": value.binary} if value.binary is not None else {"SecretString": value.string}

    async def create(self, name: str, value: AwsValue, token: str, tags: dict[str, str], description: str) -> SecretMeta:
        if not self.name_allowed(name):
            raise ConnectorError("forbidden", f"{name} is outside the allowed prefixes {', '.join(self.prefixes)}")
        all_tags = {**self.required_tags, **tags}
        kwargs: dict[str, Any] = {"Name": name, "ClientRequestToken": token, "Description": description[:2048],
                                  "Tags": [{"Key": k, "Value": v} for k, v in sorted(all_tags.items())], **self._payload(value)}
        if self.kms_key_id:
            kwargs["KmsKeyId"] = self.kms_key_id
        await self._call("create_secret", f"create {name}", **kwargs)
        meta = await self.describe(name)
        if meta is None:
            raise ConnectorError("unavailable", f"{name} was created but cannot be read back yet", retryable=True)
        return meta

    async def put_pending(self, meta: SecretMeta, value: AwsValue, token: str) -> str:
        """A new version labelled MOKSPENDING only. AWSCURRENT does not move."""
        self.check(meta)
        r = await self._call("put_secret_value", f"write a new version of {meta.name}", SecretId=meta.arn, ClientRequestToken=token,
                             VersionStages=[PENDING], **self._payload(value))
        return r["VersionId"]

    async def promote(self, meta: SecretMeta, new_version: str, expected_current: str) -> None:
        """Move AWSCURRENT to new_version only while it is still on expected_current (AWSPREVIOUS follows), then drop MOKSPENDING."""
        self.check(meta)
        await self._call("update_secret_version_stage", f"make the new version of {meta.name} current", SecretId=meta.arn, VersionStage=CURRENT,
                         MoveToVersionId=new_version, RemoveFromVersionId=expected_current)
        await self.clear_pending(meta, new_version)

    async def clear_pending(self, meta: SecretMeta, version: str) -> None:
        try:
            await self._call("update_secret_version_stage", f"remove {PENDING} from {meta.name}", SecretId=meta.arn, VersionStage=PENDING, RemoveFromVersionId=version)
        except ConnectorError as exc:
            if exc.kind != "conflict":  # already gone
                raise

    async def restage(self, meta: SecretMeta, to_version: str, expected_current: str) -> None:
        """Rollback: make an existing version current again. No new version is written."""
        self.check(meta)
        await self._call("update_secret_version_stage", f"roll {meta.name} back", SecretId=meta.arn, VersionStage=CURRENT,
                         MoveToVersionId=to_version, RemoveFromVersionId=expected_current)

    async def delete(self, meta: SecretMeta) -> str | None:
        """Scheduled deletion with the configured recovery window. Never ForceDeleteWithoutRecovery."""
        self.check(meta)
        if not self.allow_delete:
            raise ConnectorError("forbidden", "this connector does not allow deleting AWS secrets (config.allow_delete)")
        r = await self._call("delete_secret", f"schedule deletion of {meta.name}", SecretId=meta.arn, RecoveryWindowInDays=self.recovery_window_days)
        return _iso(r.get("DeletionDate"))
