"""Authorization that follows changes while work is running.

Roles are read fresh rather than trusted from when an investigation started. The cheap check
compares versions that database triggers maintain (users.auth_version, environments.policy_version)
and reloads roles only when one has moved.

Maximum delay: CHECK_SECONDS for a running investigation or an open event stream, and none for a new
tool call or LLM request, which check first. Stopping work cancels requests in flight, but data
already sent to an external system, such as a prompt that reached an LLM provider or a query that
reached Prometheus, cannot be recalled.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select

from ..db import session_scope
from ..models import Environment, RoleBinding, User
from ..rbac import Binding, P, Principal
from ..scope import ScopeError, effective_scope

CHECK_SECONDS = 5  # documented in docs/SECURITY.md as the maximum revocation delay


class AccessRevoked(ScopeError):
    pass


class AccessGuard:
    def __init__(self, user_id: uuid.UUID, environment_id: uuid.UUID, namespace: str, permission: P, ctx=None):
        self.user_id, self.environment_id, self.namespace, self.permission, self.ctx = user_id, environment_id, namespace, permission, ctx
        self.seen: tuple[int, int] | None = None

    async def check(self) -> None:
        async with session_scope() as db:
            row = (await db.execute(select(User.is_active, User.auth_version, User.email).where(User.id == self.user_id))).first()
            env = await db.get(Environment, self.environment_id)
            if row is None or not row[0] or env is None:
                raise AccessRevoked("this account was disabled or removed")
            versions = (row[1], env.policy_version)
            if versions == self.seen:
                return
            rows = (await db.execute(select(RoleBinding).where(RoleBinding.user_id == self.user_id))).scalars().all()
            principal = Principal(str(self.user_id), row[2], [Binding(r.role, r.environment, r.namespace) for r in rows])
            try:
                scope = effective_scope(principal, env, self.permission)
                if self.namespace != "*":
                    scope.require(self.namespace)
                elif not scope.cluster_wide and not scope.user_patterns:
                    raise ScopeError("no namespaces in scope")
            except ScopeError as exc:
                raise AccessRevoked(str(exc)) from None
            self.seen = versions
            if self.ctx is not None:
                self.ctx.principal, self.ctx.scope = principal, scope
