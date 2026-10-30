---
name: lambda-errors
title: Lambda errors and throttling
version: '1'
owner: sre
category: aws
description: Explain Lambda errors, throttles and duration limits.
triggers:
  keywords:
  - lambda
  - function errors
  - throttles
  - cold start
  findings: []
steps:
- tool: cw_metric
  args:
    namespace: AWS/Lambda
    metric: Errors
    stat: Sum
  purpose: Errors from CloudWatch
- tool: cw_metric
  args:
    namespace: AWS/Lambda
    metric: Throttles
    stat: Sum
  purpose: Throttles from CloudWatch
- tool: cw_metric
  args:
    namespace: AWS/Lambda
    metric: Duration
    stat: p95
  purpose: Duration from CloudWatch
resource_types:
- Lambda
hypotheses:
- statement: Concurrency is exhausted.
  supports: Throttles above zero.
  refutes: No throttles.
---
# Lambda errors and throttling

Throttles are a concurrency limit and Errors are code failures. Duration near the timeout means the function is being killed.
