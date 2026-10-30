---
name: escalation-handover
title: Escalation and handover
version: '1'
owner: sre
category: support
description: 'Prepare a clean handover: what is known, what is not, what was tried, and who owns the next step.'
triggers:
  keywords:
  - handover
  - escalate
  - escalation
  - hand off
  - shift change
  findings: []
steps:
- tool: prom_alerts
  args:
    firing_only: true
  purpose: current alerts
resource_types:
- Application service
---
# Escalation and handover

State facts, open questions, actions taken and their results, and the next step with an owner. Never let a hypothesis read as a fact.
