---
name: alb-target-health
title: ALB target health
version: '1'
owner: sre
category: aws
description: Explain unhealthy targets behind an Application Load Balancer.
triggers:
  keywords:
  - alb
  - load balancer
  - target group
  - unhealthy targets
  - target health
  findings: []
steps:
- tool: cw_metric
  args:
    namespace: AWS/ApplicationELB
    metric: UnHealthyHostCount
    stat: Maximum
  purpose: UnHealthyHostCount from CloudWatch
- tool: cw_metric
  args:
    namespace: AWS/ApplicationELB
    metric: HTTPCode_ELB_5XX_Count
    stat: Sum
  purpose: HTTPCode_ELB_5XX_Count from CloudWatch
- tool: cw_alarms
  args:
    state: ALARM
  purpose: CloudWatch alarms in ALARM
resource_types:
- Load balancer
- Target group
hypotheses:
- statement: Health checks fail.
  supports: Targets unhealthy with a failing check path.
  refutes: Targets healthy.
detection:
- id: alb-alarm
  kind: cloudwatch_alarm
  query: (?i)(alb|elb|target|unhealthy)
  sustained_checks: 1
  severity: sev2
  title: Load balancer alarm
---
# ALB target health

ELB 5XX comes from the balancer and target 5XX from your app. They point at different owners.
