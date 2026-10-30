"""FinOps: AWS costs, forecast, anomalies, budgets; Kubernetes allocation and rightsizing; MOKS's own LLM spend."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.deps import get_auth, needs
from ..auth.sessions import AuthContext
from ..db import get_db
from ..models import LLMUsage
from ..rbac import P
from .common import live_executor, outcome_response

router = APIRouter(prefix="/finops", tags=["finops"])


async def _run(request, db, ctx, env, tool, args, perm=P.FINOPS_READ) -> dict:
    ex = await live_executor(request, db, ctx, env, None, perm=perm)
    return outcome_response(await ex.run(tool, args))


@router.get("/{environment}/aws/costs")
async def aws_costs(environment: str, request: Request, group_by: str = "service", days: int = Query(14, ge=1, le=90), tag_key: str | None = None,
                    ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    args: dict[str, Any] = {"group_by": group_by, "days": days}
    if tag_key:
        args["tag_key"] = tag_key
    return await _run(request, db, ctx, environment, "finops_aws_costs", args)


@router.get("/{environment}/aws/forecast")
async def aws_forecast(environment: str, request: Request, days: int = Query(30, ge=1, le=90), ctx: AuthContext = Depends(get_auth),
                       db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    return await _run(request, db, ctx, environment, "finops_aws_forecast", {"days": days})


@router.get("/{environment}/aws/anomalies")
async def aws_anomalies(environment: str, request: Request, days: int = Query(30, ge=1, le=90), ctx: AuthContext = Depends(get_auth),
                        db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    return await _run(request, db, ctx, environment, "finops_aws_anomalies", {"days": days})


@router.get("/{environment}/aws/budgets")
async def aws_budgets(environment: str, request: Request, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    return await _run(request, db, ctx, environment, "finops_budgets", {})


@router.get("/{environment}/k8s/allocation")
async def allocation(environment: str, request: Request, namespace: str, days: int = Query(7, ge=1, le=30),
                     ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    return await _run(request, db, ctx, environment, "finops_k8s_allocation", {"namespace": namespace, "days": days})


@router.get("/{environment}/k8s/rightsizing")
async def rightsizing(environment: str, request: Request, namespace: str, days: int = Query(7, ge=1, le=30),
                      ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    return await _run(request, db, ctx, environment, "finops_rightsizing", {"namespace": namespace, "days": days})


@router.get("/llm")
async def llm_spend(days: int = Query(30, ge=1, le=365), ctx: AuthContext = Depends(needs(P.ADMIN_LLM)), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    day = func.date_trunc("day", LLMUsage.created_at)
    rows = (await db.execute(select(day, LLMUsage.provider, LLMUsage.model, func.sum(LLMUsage.tokens_in), func.sum(LLMUsage.tokens_out),
                                    func.sum(LLMUsage.cost_usd), func.count()).where(LLMUsage.created_at >= since)
                             .group_by(day, LLMUsage.provider, LLMUsage.model).order_by(day))).all()
    by_model: dict[str, list] = {}
    total = 0.0
    for d, prov, model, tin, tout, cost, n in rows:
        key = f"{prov}/{model}"
        by_model.setdefault(key, []).append([int(d.timestamp()), float(cost or 0)])
        total += float(cost or 0)
    return {"days": days, "total_usd": round(total, 4), "series": [{"metric": {"model": k}, "values": v} for k, v in by_model.items()],
            "rows": [{"day": d.date().isoformat(), "model": f"{p}/{m}", "tokens_in": int(ti or 0), "tokens_out": int(to or 0),
                      "cost_usd": round(float(c or 0), 4), "calls": n} for d, p, m, ti, to, c, n in rows]}
