---
name: dns-resolution
description: "Diagnose cluster DNS problems: CoreDNS health, kube-dns endpoints and DNS-related events."
triggers:
  keywords: [dns, resolve, resolution, nxdomain, no such host, lookup, coredns, servfail, name resolution]
  findings: [event_DNSConfigForming]
steps:
  - tool: k8s_dns_health
    purpose: CoreDNS pods and kube-dns endpoints
  - tool: loki_search
    args: {namespace: "{namespace}", contains: "no such host", limit: 50}
    purpose: resolution failures in application logs
  - tool: k8s_events
    args: {namespace: "{namespace}", warnings_only: true}
    purpose: DNSConfigForming and related warnings
title: DNS resolution
owner: sre
category: kubernetes
version: '2'
---
# DNS resolution

- **CoreDNS down or not ready** → every lookup in the cluster fails; check `k8s_dns_health` first.
- **Only some names fail** → wrong service name/namespace (use `svc.ns.svc.cluster.local`), or the Service has no endpoints (that is a routing problem, see http-5xx-service-routing).
- **External names fail** → upstream resolver or egress NetworkPolicy blocking port 53.
- `DNSConfigForming` events mean the pod's resolv.conf exceeded limits (too many search domains/nameservers).

A diagnostic session (`dns_lookup`, `tcp_check`) confirms resolution from inside the cluster network.
