"""FinOps: AWS costs, forecast, anomalies, budgets; Kubernetes allocation and rightsizing; MOKS's own LLM spend."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import audit
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


async def _llm_rows(db: AsyncSession, days: int, envs: list[str] | None):
    from ..models import Environment, Investigation

    since = datetime.now(timezone.utc) - timedelta(days=days)
    day = func.date_trunc("day", LLMUsage.created_at)
    env_name = func.coalesce(Environment.name, "(admin tests)")
    q = (select(day, LLMUsage.provider, LLMUsage.model, env_name, func.sum(LLMUsage.tokens_in), func.sum(LLMUsage.tokens_out),
                func.sum(LLMUsage.cost_usd), func.count())
         .select_from(LLMUsage).outerjoin(Investigation, Investigation.id == LLMUsage.investigation_id)
         .outerjoin(Environment, Environment.id == Investigation.environment_id)
         .where(LLMUsage.created_at >= since).group_by(day, LLMUsage.provider, LLMUsage.model, env_name).order_by(day))
    if envs is not None:
        q = q.where(Environment.name.in_(envs))
    return (await db.execute(q)).all()


@router.get("/llm")
async def llm_spend(days: int = Query(30, ge=1, le=365), environment: str = "all", ctx: AuthContext = Depends(get_auth),
                    db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    """LLM calls made by MOKS: tokens and estimated cost by day, model and environment.
    finops.read users see their environments; LLM administrators also see calls outside investigations."""
    from ..models import Environment

    names = [e.name for e in (await db.execute(select(Environment))).scalars()]
    allowed = ctx.principal.environments_for(P.FINOPS_READ, names)
    if not allowed and not ctx.principal.can(P.ADMIN_LLM, "*", "*"):
        raise HTTPException(403, "You need finops.read")
    envs = None if ctx.principal.can(P.ADMIN_LLM, "*", "*") and environment == "all" else [e for e in allowed if environment in ("all", e)]
    rows = await _llm_rows(db, days, envs)
    return _llm_out(rows, days)


def _llm_out(rows, days: int) -> dict:
    by_model: dict[str, list] = {}
    model_tot: dict[str, dict] = {}
    env_tot: dict[str, float] = {}
    total = 0.0
    for d, prov, model, env, tin, tout, cost, n in rows:
        key = f"{prov}/{model}"
        by_model.setdefault(key, []).append([int(d.timestamp()), float(cost or 0)])
        m = model_tot.setdefault(key, {"model": key, "calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0})
        m["calls"] += n
        m["tokens_in"] += int(tin or 0)
        m["tokens_out"] += int(tout or 0)
        m["cost_usd"] = round(m["cost_usd"] + float(cost or 0), 6)
        env_tot[env] = round(env_tot.get(env, 0) + float(cost or 0), 6)
        total += float(cost or 0)
    return {"days": days, "total_usd": round(total, 4), "series": [{"metric": {"model": k}, "values": v} for k, v in by_model.items()],
            "by_model": sorted(model_tot.values(), key=lambda m: -m["cost_usd"]), "by_environment": env_tot,
            "calls": sum(m["calls"] for m in model_tot.values()), "tokens": sum(m["tokens_in"] + m["tokens_out"] for m in model_tot.values()),
            "note": "Cost uses the per-million-token prices configured on each LLM provider; 0 means no price is configured."}


@router.get("/overview")
async def overview(request: Request, environment: str = "all", namespace: str | None = None, days: int = Query(7, ge=1, le=30),
                   ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    """All environments by default: Kubernetes cost by resource type (cpu, memory, storage), AWS spend
    and LLM calls, per environment and in total. Each part reports its own error instead of failing."""
    import asyncio

    from ..models import Environment

    envs = [e.name for e in (await db.execute(select(Environment).where(~Environment.name.startswith("eval-")).order_by(Environment.name))).scalars()]
    names = [n for n in ctx.principal.environments_for(P.FINOPS_READ, envs) if environment in ("all", n)]
    if not names:
        raise HTTPException(403, "You need finops.read in at least one environment")
    per_env: list[dict] = []
    totals = {"cpu": 0.0, "memory": 0.0, "storage": 0.0, "kubernetes": 0.0, "idle": 0.0, "aws": 0.0, "llm": 0.0}
    for name in names:
        row: dict = {"environment": name, "errors": {}}
        ex = await live_executor(request, db, ctx, name, None, perm=P.FINOPS_READ, audit_success=False)
        if ex.ctx.available("cost_model") and ex.ctx.available("kubernetes") and ex.ctx.available("prometheus"):
            args = {"days": days, **({"namespace": namespace} if namespace else {})}
            alloc, rs = await asyncio.gather(ex.run("finops_k8s_allocation", args), ex.run("finops_rightsizing", args))
            if alloc.ok:
                row["kubernetes"] = {k: alloc.data[k] for k in ("total", "idle", "by_resource", "by_namespace", "by_kind", "unit_prices", "cost_model", "namespaces")}
                row["workloads"] = alloc.data["workloads"]
                for r in ("cpu", "memory", "storage"):
                    totals[r] += alloc.data["by_resource"][r]
                totals["kubernetes"] += alloc.data["total"]
                totals["idle"] += alloc.data["idle"]
                if alloc.data.get("skipped_namespaces"):
                    row["errors"]["skipped namespaces"] = ", ".join(sorted(alloc.data["skipped_namespaces"]))
            else:
                row["errors"]["kubernetes"] = alloc.summary
            if rs.ok:
                row["rightsizing"] = rs.data["recommendations"]
                row["monthly_saving"] = rs.data["monthly_saving"]
            else:
                row["errors"]["rightsizing"] = rs.summary
        else:
            row["errors"]["kubernetes"] = "needs kubernetes, prometheus and cost_model connectors"
        if ex.ctx.available("aws_cost"):
            aws = await ex.run("finops_aws_costs", {"group_by": "service", "days": max(days, 7)})
            if aws.ok:
                row["aws"] = {"total": aws.data["total"], "unit": aws.data["unit"], "groups": aws.data["groups"][:15], "series": aws.data["series"]}
                totals["aws"] += aws.data["total"]
            else:
                row["errors"]["aws"] = aws.summary
        per_env.append(row)
    llm = _llm_out(await _llm_rows(db, days, names), days)
    totals["llm"] = llm["total_usd"]
    totals = {k: round(v, 4) for k, v in totals.items()}
    totals["all"] = round(totals["kubernetes"] + totals["aws"] + totals["llm"], 4)
    await audit.record(db, action="finops.overview", actor_id=ctx.user.id, actor_email=ctx.user.email, decision="allow",
                       details={"environments": names, "namespace": namespace, "days": days})
    return {"days": days, "environments": per_env, "totals": totals, "llm": llm}


@router.post("/analyze", status_code=201)
async def analyze(request: Request, body: dict, ctx: AuthContext = Depends(get_auth), db: AsyncSession = Depends(get_db, scope="function")) -> dict:
    """Ask the investigator (LLM + cost-spike-finops / config-rightsizing-hygiene skills) for a cited
    FinOps review. Returns the investigation id; progress streams like any investigation."""
    from .investigations import CreateIn, create

    env = body.get("environment") if body.get("environment") not in (None, "", "all") else None
    ns = body.get("namespace") or None
    focus = body.get("focus") or "costs, idle capacity and rightsizing"
    q = (f"FinOps review of {('namespace ' + ns) if ns else 'all namespaces'}: analyse {focus} over the last {int(body.get('days', 7))} days. "
         "Break cost down by resource type (cpu, memory, storage), cloud services and LLM usage, rank the biggest savings and the risks "
         "(OOM, throttling, missing requests), and propose concrete rightsizing and configuration changes.")
    return await create(CreateIn(question=q, environment=env, namespace=ns, minutes=60), request, ctx, db)
    by_model: dict[str, list] = {}
    total = 0.0
    for d, prov, model, tin, tout, cost, n in rows:
        key = f"{prov}/{model}"
        by_model.setdefault(key, []).append([int(d.timestamp()), float(cost or 0)])
        total += float(cost or 0)
    return {"days": days, "total_usd": round(total, 4), "series": [{"metric": {"model": k}, "values": v} for k, v in by_model.items()],
            "rows": [{"day": d.date().isoformat(), "model": f"{p}/{m}", "tokens_in": int(ti or 0), "tokens_out": int(to or 0),
                      "cost_usd": round(float(c or 0), 4), "calls": n} for d, p, m, ti, to, c, n in rows]}
