# FinOps

MOKS answers cost questions with the same evidence discipline as incidents: every number cites the
query, window and cost model it came from. All FinOps tools are read-only and need the
`finops.read` permission (investigator, operator, approver, admin).

| Capability | Source | Connector |
|---|---|---|
| Spend by service, account, region, usage type or allocation tag | AWS Cost Explorer `GetCostAndUsage` | `aws_cost` |
| Forecast | Cost Explorer `GetCostForecast` | `aws_cost` |
| Anomalies with root causes | Cost Anomaly Detection `GetAnomalies` | `aws_cost` |
| Budgets (actual and forecast vs limit) | AWS Budgets | `aws_cost` |
| Cost per workload, idle cost, efficiency | requests (Kubernetes) + usage (cAdvisor via Prometheus) × unit prices | `cost_model` + `kubernetes` + `prometheus` |
| Rightsizing (requests/limits) with savings and OOM/throttling risk | p95 CPU and max memory over the window | same |
| MOKS's own LLM spend | `llm_usage` table | none |

## FinOps page

The page opens on all environments and needs no namespace. Filter by environment, then optionally
by namespace, workload or kind. It shows:

- totals by resource type: CPU, memory, storage, cloud services and LLM calls, per environment
- LLM calls by model (calls, input and output tokens, cost). Cost uses the prices set on each LLM
  provider, so 0 means no price is set, not free
- cost per workload, split by CPU, memory and storage, with idle cost
- rightsizing across environments, with **Propose** to open an action plan
- AWS anomalies, budgets and forecast, when one environment with `aws_cost` is selected

A namespace that cannot be read is skipped and listed; the rest of the report still loads.

**Analyze with AI** starts an investigation with the FinOps question. It uses the active LLM and the
`cost-spike-finops` and `config-rightsizing-hygiene` skills, and cites its evidence.

## Cost model

Kubernetes capacity has no bill of its own, so you tell MOKS what it costs:

```yaml
- type: cost_model
  name: hetzner
  config: {monthly_cost_usd: 45, cpu_share: 0.5, currency: USD}   # fixed-price cluster (mk4)
# or, cloud nodes priced per unit:
#  config: {cpu_hour_usd: 0.031, gib_hour_usd: 0.004}
```

With a fixed monthly price, the hourly price is split between CPU and memory (`cpu_share`) and spread
over the cluster's allocatable capacity. Each workload is charged on **max(request, usage)**,
because requested capacity is reserved whether it is used or not. **Idle cost** is the requested
but unused part. The mk4 values ship `monthly_cost_usd: 0`; until you set the real price, MOKS
shows efficiency and rightsizing but no money.

## Rightsizing rules

- Recommendation: CPU request = p95 × 1.25, memory request = max working set × 1.25 (minimum 10m / 32Mi).
- **increase** first: memory within 10% of its limit (OOM risk), CPU p95 above the request
  (throttling), or missing requests.
- **reduce** only when the monthly saving is ≥ 0.5 and requests exist; never below observed peaks.
- "Propose" turns a recommendation into a `patch_resources` action plan. That plan is reviewed,
  approved in production, executed by the executor, validated (rollout, no crashing pods) and
  rolled back automatically if validation fails.

Investigations use the same data. For example, "which pods are close to their memory limits?"
selects the `oomkilled-memory` skill, which runs rightsizing and turns at-risk containers into
`memory_near_limit` findings with a `patch_resources` suggestion.

## AWS setup

1. Terraform: set `finops_enabled = true` (already in `tfvars/mk4.tfvars`) and apply. The
   `moks-mk4-runtime` user gets `ce:GetCostAndUsage/GetCostForecast/GetAnomalies` and
   `budgets:ViewBudget`, read-only.
2. Make sure Cost Explorer is enabled for the account. Cost Anomaly Detection needs at least one
   monitor (AWS creates a default services monitor).
3. Add the connector (Connectors → `aws_cost`, or uncomment it in `values-minikube.yaml`).
   Allocation tags must be activated as cost allocation tags in Billing before grouping by them.

**Cost of the tool itself:** Cost Explorer charges $0.01 per API request. MOKS caches identical
queries (`cache_minutes`, default 60) and refuses more than `max_requests_per_hour` (default 60),
which caps it at $0.60 per hour.

## Limits

- Cost Explorer data lags up to 24 hours. Same-day questions get a stale answer, and the report says so.
- Kubernetes allocation covers pods that exist now. Deleted pods in the window are not charged.
- Usage comes from cAdvisor (`container_cpu_usage_seconds_total`, `container_memory_working_set_bytes`);
  without Prometheus, allocation and rightsizing are unavailable.
- There is no kube-state-metrics on mk4, so requests come from the live pod specs.
