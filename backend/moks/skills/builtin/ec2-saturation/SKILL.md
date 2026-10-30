---
name: ec2-saturation
title: EC2 resource saturation
version: '1'
owner: sre
category: aws
description: Find EC2 instances saturated on CPU, credits or network.
triggers:
  keywords:
  - ec2
  - instance
  - cpu credits
  - saturation
  findings: []
steps:
- tool: cw_metric
  args:
    namespace: AWS/EC2
    metric: CPUUtilization
    stat: Maximum
  purpose: CPUUtilization from CloudWatch
- tool: cw_metric
  args:
    namespace: AWS/EC2
    metric: StatusCheckFailed
    stat: Maximum
  purpose: StatusCheckFailed from CloudWatch
- tool: cw_alarms
  args:
    state: ALARM
  purpose: CloudWatch alarms in ALARM
resource_types:
- EC2
hypotheses:
- statement: The instance is out of CPU credits.
  supports: CPUCreditBalance near zero.
  refutes: Fixed-performance type.
---
# EC2 resource saturation

EC2 does not report memory by default. Do not read missing data as healthy.
