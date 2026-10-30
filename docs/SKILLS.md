# SRE skills (`SKILL.md`)

Skills are reusable investigation playbooks. Each one is a directory holding a `SKILL.md` file:
YAML frontmatter describes *when* the skill applies and *which* read-only steps it runs, and a
Markdown body tells the investigator how to interpret the evidence and what not to conclude.

For each investigation, MOKS:

1. **Selects** up to three skills, ranking matching detector findings (×3) above question keywords (×1).
2. **Runs** their steps through the ToolExecutor, with the same RBAC, scope, bounds, redaction and
   audit as any other tool call. A step is skipped when its connector isn't configured, the user's
   scope doesn't allow it, or a template variable is unknown (for example the Deployment of a
   StatefulSet pod).
3. **Guides the LLM:** the skill body is added to the planner and report prompts as *trusted*
   instructions. In rules mode, the steps themselves are the playbook.
4. **Suggests actions:** `remediation` hints whose `when` matches a finding become `skill_actions` in
   the report. Each one is a prefilled, allowlisted action plan suggestion that still needs review
   (and approval in production).

The report lists the skills applied and why. Progress events show `[skill] purpose` for every step.

## Format

```markdown
---
name: crashloop-triage                       # unique, lowercase
description: "One sentence: what it diagnoses."   # quote it if it contains a colon
version: "1"
triggers:
  keywords: [crash, crashloop, restarting]  # whole-word prefix match in the question
  findings: [crashloop, config_error]       # detector finding kinds
steps:
  - tool: k8s_pod_logs                      # must be a registered read-only tool
    for_each: finding.crashloop             # one call per matching finding (omit for a single call)
    limit: 2                                # max iterations (1-5)
    args: {namespace: "{namespace}", pod: "{pod}", previous: true, tail_lines: 150}
    purpose: output of the crashed container
    when_question: "limit|close"            # optional regex on the question
remediation:
  - when: crashloop                         # finding kind
    action: rollback_deployment             # must be allowlisted (docs/SECURITY.md)
    target: "Deployment/{deployment}"
    params: {}
    note: Only if a rollout preceded the first crash.
---
# Runbook text the investigator follows
```

Template variables: `{namespace}`, `{pod}`, `{workload}`, `{deployment}`, `{service}`, `{resource}`
(from the finding being iterated), `{question}` and `{trace_id}` (from the question).

Validation runs at load. A skill is **rejected** (and listed under errors on the Skills page) if:
the frontmatter is invalid; a step names an unknown or mutating tool; a template uses an unknown
variable; a remediation names a non-allowlisted action; or the body exceeds 12,000 characters.

## Built-in skills

| Skill | Activates on | Does |
|---|---|---|
| `crashloop-triage` | crashloop, recent restarts, config errors | previous-container logs, pod config, rollout timing, runbooks |
| `oomkilled-memory` | OOMKilled, memory near limit | limits vs working set, namespace-wide memory headroom, rightsizing |
| `http-5xx-service-routing` | 502/503/504, selector mismatch, no endpoints | ingress → service → endpoints → pods, 5xx logs |
| `latency-regression` | latency/slow/timeout, recent rollouts | rollouts of service and dependencies, throttling, timeout logs |
| `pending-pods-scheduling` | Pending, FailedScheduling/Mount | scheduler message, events, node capacity and taints |
| `image-pull-failures` | ErrImagePull/ImagePullBackOff | image reference, registry error, revision that introduced it |
| `dns-resolution` | DNS/NXDOMAIN/lookup | CoreDNS health, resolution errors in logs |
| `node-pressure-evictions` | Evicted, node pressure | node conditions, eviction messages, scrape health |
| `readiness-probe-failures` | not ready, Unhealthy | probe definitions, Unhealthy events, app logs |
| `alert-triage` | alert/firing | rule expression and value, Grafana rules, dashboards, runbook |
| `trace-id-correlation` | trace/request ID | every log line for the trace, surrounding errors |
| `deployment-drift-git` | git/version/drift, recent rollouts | running revisions vs Git; never guesses a commit |
| `cost-spike-finops` | cost/bill/budget/waste | anomalies, spend by service, budgets, allocation, rightsizing |

They live in `backend/moks/skills/builtin/` and are packaged into the image.

## Custom skills

Add them through Helm (`customSkills`). They are mounted from a ConfigMap at `/etc/moks-skills`, and
a custom skill with a built-in's name replaces it:

```yaml
customSkills:
  checkout-runbook: |
    ---
    name: checkout-runbook
    description: "Checkout-specific triage: payment provider timeouts and cart dependencies."
    triggers: {keywords: [checkout, payment], findings: [service_no_ready_endpoints]}
    steps:
      - {tool: loki_search, args: {namespace: "{namespace}", contains: "payment timeout", limit: 50}, purpose: provider timeouts}
      - {tool: kb_search, args: {query: "checkout payment provider runbook"}}
    ---
    # Checkout
    The payment provider times out under load; check `payment timeout` lines before blaming checkout itself. ...
```

After a ConfigMap change, roll the API and worker pods, or use **SRE skills → Reload**
(administrators; recorded in the audit log).

## Trust model

Skills are instructions to the investigator, so treat them like code. They come only from the image
and a reviewed ConfigMap (GitOps), never from the knowledge base, logs or tool output; text from
those sources is always fenced as untrusted data. The worst a bad skill can do is run allowed
read-only tools within the user's scope, or suggest an allowlisted action that still needs review
and approval.

## Testing a skill

`tests/test_skills.py` covers loading, validation, selection and end-to-end execution. The lab
evaluation (`moks eval lab`) runs every scenario with skills active. Add a dataset case to
`backend/moks/evals/datasets/` for each new skill you write.

## Versioned metadata and detection

Skills also carry `title`, `owner`, `category`, `resource_types`, `requires`, `telemetry`, `queries`,
`hypotheses`, `evidence_required`, `verification`, `recovery_validation`, `runbooks`, `evaluation` and
`detection`. All are validated at load. A skill cannot grant permissions, and detection rules have no
action field.

```yaml
detection:
  - id: pod-crashloop
    kind: finding            # finding | prometheus | prometheus_alert | loki | cloudwatch_alarm
    finding: crashloop
    interval_minutes: 5
    lookback_minutes: 15
    sustained_checks: 2      # consecutive checks before an incident opens
    recovery_checks: 3       # consecutive clear checks before it counts as clear
    severity: sev2
    cooldown_minutes: 30     # returning inside this reopens the incident (flapping)
    auto_investigate: true
    auto_resolve: true
    empty_means_ok: false    # no data is unknown unless the rule says otherwise
```

A skill that needs a connector an environment lacks is skipped, and the report and the Skills page say why.

