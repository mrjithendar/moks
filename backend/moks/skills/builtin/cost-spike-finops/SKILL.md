---
name: cost-spike-finops
description: "Investigate cost increases and waste: AWS anomalies and spend by service, Kubernetes idle cost and rightsizing."
triggers:
  keywords: [cost, costs, spend, spending, bill, billing, budget, finops, expensive, waste, idle, rightsizing, savings]
  findings: []
steps:
  - tool: finops_aws_anomalies
    args: {days: 30}
    purpose: Cost Anomaly Detection findings with root causes
  - tool: finops_aws_costs
    args: {group_by: service, days: 14}
    purpose: daily spend by service
  - tool: finops_budgets
    purpose: budgets at risk
  - tool: finops_k8s_allocation
    args: {namespace: "{namespace}", days: 7}
    purpose: cost and idle cost per workload
  - tool: finops_rightsizing
    args: {namespace: "{namespace}", days: 7}
    purpose: request changes with savings and OOM risk
remediation:
  - when: cost
    action: patch_resources
    note: Apply rightsizing recommendations one workload at a time and validate; never reduce below observed p95/max.
---
# Cost spikes and waste

1. **Anomalies first:** Cost Anomaly Detection names service, region and usage type — quote them with the impact.
2. **Trend by service:** find the day the curve bends; correlate with deployments or traffic.
3. **Kubernetes:** idle cost = requested but unused capacity; high idle with low efficiency is waste,
   but *under*-provisioned workloads (OOM/throttling risk) must be fixed before any reduction.
4. Every saving claim must cite the query window and the cost model used (fixed monthly price vs cloud rates).

Cost data lags up to 24 h in Cost Explorer; say so when the question is about the last few hours.
