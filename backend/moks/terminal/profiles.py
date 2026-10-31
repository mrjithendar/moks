"""What a terminal may be opened against. Profiles come from the chart, not the database, so pointing
terminals at a stronger IAM role is a reviewed change and not something an admin can do in the UI."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from ..config import get_settings

_ROLE = re.compile(r"arn:aws[a-z-]*:iam::(\d{12}):role/[\w+=,.@/-]{1,512}")
_REGION = re.compile(r"[a-z]{2}(-[a-z]+)+-\d")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
IN_CLUSTER = "in-cluster"


class ProfileError(ValueError):
    pass


@dataclass(frozen=True)
class Profile:
    name: str
    environment: str
    account: str
    role_arn: str
    regions: tuple[str, ...]
    eks_clusters: tuple[str, ...] = ()
    in_cluster: bool = False
    label: str = ""
    partition: str = field(default="aws")

    def clusters(self) -> list[str]:
        return ([IN_CLUSTER] if self.in_cluster else []) + list(self.eks_clusters)


def parse(text: str) -> list[Profile]:
    try:
        rows = json.loads(text or "[]")
    except ValueError as exc:
        raise ProfileError(f"MOKS_TERMINAL_PROFILES is not JSON: {exc}") from None
    if not isinstance(rows, list):
        raise ProfileError("MOKS_TERMINAL_PROFILES must be a list")
    out: list[Profile] = []
    for row in rows:
        name, role = str(row.get("name") or ""), _ROLE.fullmatch(str(row.get("roleArn") or ""))
        account = str(row.get("account") or "")
        regions = tuple(str(r) for r in row.get("regions") or [])
        clusters = tuple(str(c) for c in row.get("eksClusters") or [])
        if not _NAME.fullmatch(name):
            raise ProfileError(f"terminal profile name {name!r} is not valid")
        if not role or role.group(1) != account:
            raise ProfileError(f"terminal profile {name}: roleArn must be a role in account {account!r}")
        if not row.get("environment"):
            raise ProfileError(f"terminal profile {name}: environment is required")
        if not regions or not all(_REGION.fullmatch(r) for r in regions):
            raise ProfileError(f"terminal profile {name}: regions must list valid AWS regions")
        if not all(_NAME.fullmatch(c) and c != IN_CLUSTER for c in clusters):
            raise ProfileError(f"terminal profile {name}: eksClusters holds an invalid name")
        out.append(Profile(name, str(row["environment"]), account, role.group(0), regions, clusters, bool(row.get("inCluster")),
                           str(row.get("label") or name), role.group(0).split(":")[1]))
    if len({p.name for p in out}) != len(out):
        raise ProfileError("terminal profile names must be unique")
    return out


def profiles() -> list[Profile]:
    return parse(get_settings().terminal_profiles)


def find(name: str) -> Profile | None:
    return next((p for p in profiles() if p.name == name), None)


def kube_namespaces() -> list[str]:
    return [n for n in get_settings().terminal_kube_namespaces.split(",") if n]
