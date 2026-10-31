"""Short-lived AWS credentials for one session. Held in memory only, and only long enough to hand
to the pod. Any failure ends the attempt: there is no second identity to fall back to."""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .profiles import Profile


class TerminalError(Exception):
    """Something a person can act on. The message is safe to show and to store."""


@dataclass(repr=False)
class Credentials:
    access_key: str
    secret_key: str
    token: str
    expires_at: datetime

    def __repr__(self) -> str:  # never let a traceback or log line carry them
        return f"Credentials(expires_at={self.expires_at.isoformat()})"

    def shared_file(self, profile: str = "default") -> bytes:
        return (f"[{profile}]\naws_access_key_id = {self.access_key}\naws_secret_access_key = {self.secret_key}\n"
                f"aws_session_token = {self.token}\n").encode()


def source_identity(email: str) -> str:
    """Stays with the session in CloudTrail, even through role chaining."""
    return re.sub(r"[^\w+=,.@-]", "-", email)[:64].ljust(2, "-")


class Aws:
    """boto3 is synchronous, so each call runs in a thread. The base identity is the default chain of
    the gateway pod, and it can do nothing except assume the terminal roles."""

    def _sts(self, region: str, creds: Credentials | None = None) -> Any:
        import boto3

        keys = {"aws_access_key_id": creds.access_key, "aws_secret_access_key": creds.secret_key, "aws_session_token": creds.token} if creds else {}
        return boto3.client("sts", region_name=region, **keys)

    async def assume(self, profile: Profile, region: str, session_name: str, seconds: int, identity: str) -> Credentials:
        def call() -> dict:
            return self._sts(region).assume_role(RoleArn=profile.role_arn, RoleSessionName=session_name, DurationSeconds=seconds, SourceIdentity=identity)
        try:
            out = (await asyncio.to_thread(call))["Credentials"]
        except Exception as exc:
            raise TerminalError(f"could not assume {profile.role_arn}: {_reason(exc)}") from None
        return Credentials(out["AccessKeyId"], out["SecretAccessKey"], out["SessionToken"], out["Expiration"])

    async def identity(self, creds: Credentials, region: str) -> dict[str, str]:
        def call() -> dict:
            return self._sts(region, creds).get_caller_identity()
        try:
            out = await asyncio.to_thread(call)
        except Exception as exc:
            raise TerminalError(f"could not verify the session's AWS identity: {_reason(exc)}") from None
        return {"account": out["Account"], "arn": out["Arn"]}

    async def trail(self, profile: Profile, region: str, audit_name: str, session_name: str, identity: str) -> list[dict]:
        """CloudTrail management events of one role session, read with the same read-only role so
        this works across accounts. CloudTrail is regional and arrives minutes late."""
        creds = await self.assume(profile, region, audit_name, 900, identity)

        def call() -> list[dict]:
            import boto3

            client = boto3.client("cloudtrail", region_name=region, aws_access_key_id=creds.access_key,
                                  aws_secret_access_key=creds.secret_key, aws_session_token=creds.token)
            return client.lookup_events(LookupAttributes=[{"AttributeKey": "Username", "AttributeValue": session_name}], MaxResults=50).get("Events", [])
        try:
            rows = await asyncio.to_thread(call)
        except Exception as exc:
            raise TerminalError(f"could not read CloudTrail: {_reason(exc)}") from None
        return [{"name": r.get("EventName"), "source": r.get("EventSource"), "at": r["EventTime"].isoformat() if r.get("EventTime") else None,
                 "read_only": r.get("ReadOnly")} for r in rows]


def _reason(exc: Exception) -> str:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code") if hasattr(exc, "response") else None
    return code or type(exc).__name__


def expected_arn(profile: Profile, session_name: str) -> str:
    role = profile.role_arn.rsplit("/", 1)[1]
    return f"arn:{profile.partition}:sts::{profile.account}:assumed-role/{role}/{session_name}"


def check_identity(found: dict[str, str], profile: Profile, session_name: str) -> None:
    """The identity must be exactly the selected one, or the session does not start."""
    if found.get("account") != profile.account or found.get("arn") != expected_arn(profile, session_name):
        raise TerminalError(f"the verified identity {found.get('arn')} is not the selected role in account {profile.account}")
