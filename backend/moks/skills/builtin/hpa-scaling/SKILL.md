---
name: hpa-scaling
title: HPA scaling problems
version: '1'
owner: sre
category: kubernetes
description: 'Explain an autoscaler that will not scale: at its maximum, missing metrics, or limited by requests.'
triggers:
  keywords:
  - hpa
  - autoscaler
  - autoscaling
  - not scaling
  - max replicas
  findings: []
steps:
- tool: prom_query
  args:
    promql: kube_horizontalpodautoscaler_status_current_replicas == kube_horizontalpodautoscaler_spec_max_replicas
    mode: instant
  purpose: autoscalers at their maximum
- tool: k8s_events
  args:
    namespace: '{namespace}'
  purpose: warning events
- tool: k8s_workloads
  args:
    namespace: '{namespace}'
  purpose: workload status
resource_types:
- HPA
- Deployment
telemetry:
- kube_horizontalpodautoscaler_status_current_replicas
hypotheses:
- statement: Demand exceeds the maximum.
  supports: At max with high utilisation.
  refutes: Utilisation below target.
- statement: Metrics are unavailable.
  supports: FailedGetResourceMetric events.
  refutes: ScalingActive true.
recovery_validation:
- current replicas below maximum, or utilisation at target
detection:
- id: hpa-at-max
  kind: prometheus
  query: (kube_horizontalpodautoscaler_status_current_replicas == kube_horizontalpodautoscaler_spec_max_replicas) * 1
  operator: '>'
  threshold: 0
  resource_label:
  - namespace
  - horizontalpodautoscaler
  sustained_checks: 6
  interval_minutes: 5
  severity: sev3
  empty_means_ok: true
  title: Autoscaler at maximum
remediation:
- when: workload_unavailable
  action: apply_config_patch
  note: Raise spec.maxReplicas within the environment's limit, after checking cluster capacity.
---
# HPA scaling problems

Read the HPA's conditions first: AbleToScale and ScalingActive say why it is not acting. An autoscaler at its maximum is a capacity decision, not a fault.
