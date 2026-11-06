import { createContext, useContext } from "react";
import { Link } from "react-router-dom";
import { qs } from "./api";
import { CopyButton } from "./ui";

/** What a claim in a report rests on. Always text plus a mark, never colour alone. */
export type ClaimKind = "observed" | "hypothesis" | "guidance" | "missing";
const CLAIM: Record<ClaimKind, [string, string]> = {
  observed: ["●", "Observed"], hypothesis: ["?", "Hypothesis"], guidance: ["§", "Documented guidance"], missing: ["!", "Missing evidence"],
};

export function ClaimLabel({ kind }: { kind: ClaimKind }) {
  const [mark, text] = CLAIM[kind];
  return <span className={`claim claim-${kind}`} data-claim={kind}><span aria-hidden="true">{mark}</span> {text}</span>;
}

/** The evidence of the report being shown, so a citation can say what it points at before it is opened. */
export const EvidenceCtx = createContext<any[]>([]);
export const useEvidence = (ref: string) => useContext(EvidenceCtx).find((e) => e.ref === ref);

export interface Target { kind: string; detail?: string; window?: string; href?: string; external?: string; hrefLabel?: string }

const when = (iso?: string) => (iso ? new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZoneName: "short" }) : "");

/** Where a piece of evidence came from, as exactly as the record allows: the query and its time range, the event's
 *  object, the Git revision, the document passage, and a link that opens the same view in MOKS where one exists. */
export function evidenceTarget(e: any): Target {
  const tool = String(e?.tool ?? "");
  const scope = e?.scope ?? {};
  const args = e?.args ?? {};
  const src = e?.source ?? {};
  const window = scope.from && scope.to ? `${when(scope.from)} – ${when(scope.to)}` : undefined;
  const minutes = scope.from && scope.to ? Math.max(5, Math.round((Date.parse(scope.to) - Date.parse(scope.from)) / 60000)) : undefined;
  if (tool.startsWith("loki")) {
    return { kind: "Log query", detail: src.query ?? args.logql, window, hrefLabel: "Open Logs in this scope",
      href: `/logs${qs({ view: "logql", environment: scope.environment, namespace: scope.namespace ?? undefined, pod: args.pod, minutes })}` };
  }
  if (tool === "k8s_pod_logs") {
    return { kind: "Pod log lines", detail: `${args.pod ?? ""}${args.container ? ` / ${args.container}` : ""}${args.previous ? " (previous container)" : ""}`, window,
      href: `/explorer${qs({ tab: "pods", env: scope.environment, ns: scope.namespace ?? undefined, q: args.pod })}`, hrefLabel: "Open the pod in the explorer" };
  }
  if (tool === "prom_alerts") return { kind: "Alert rules", detail: src.api ?? "GET /api/v1/rules?type=alert", window };
  if (tool.startsWith("prom")) return { kind: "Metric query", detail: src.query ?? args.promql, window, external: src.link, hrefLabel: "Open in Prometheus" };
  if (tool === "k8s_events") {
    return { kind: "Kubernetes events", detail: args.object_name ? `${args.object_name}` : `namespace ${scope.namespace ?? "cluster"}`, window,
      href: `/explorer${qs({ tab: "events", env: scope.environment, ns: scope.namespace ?? undefined, q: args.object_name })}`, hrefLabel: "Open events in the explorer" };
  }
  if (tool.startsWith("git")) return { kind: "Git revision", detail: [src.repository, src.revision ?? src.ref ?? args.ref, src.path ?? args.path].filter(Boolean).join(" @ "), external: src.link, hrefLabel: "Open the revision" };
  if (tool === "kb_search") return { kind: "Knowledge search", detail: args.query ?? src.query, external: src.link };
  if (tool.startsWith("knowledge") || tool.startsWith("kb_")) return { kind: "Document passage", detail: [src.path ?? src.citation?.path, src.passage ?? src.citation?.passage].filter(Boolean).join(" § "), external: src.link };
  if (tool.startsWith("k8s")) {
    return { kind: "Kubernetes read", detail: src.api ?? tool, window,
      href: `/explorer${qs({ tab: tool.includes("service") ? "services" : tool.includes("workload") || tool.includes("deployment") ? "workloads" : "pods", env: scope.environment, ns: scope.namespace ?? undefined, q: args.pod ?? args.name ?? args.object_name })}`, hrefLabel: "Open in the explorer" };
  }
  return { kind: tool || "Evidence", detail: src.api ?? src.query, window };
}

export function ExactSource({ e }: { e: any }) {
  const t = evidenceTarget(e);
  return (
    <section className="exact-source" aria-label="Exact source">
      <h3>Exact source</h3>
      <div><strong>{t.kind}</strong>{e.scope?.environment && <span className="mono small"> · {e.scope.environment}{e.scope.namespace ? `/${e.scope.namespace}` : ""}</span>}</div>
      {t.detail && <pre className="mono small" data-testid="exact-source-detail">{t.detail}</pre>}
      {t.window && <div className="small">Time range: {t.window}</div>}
      <div className="row">
        {t.detail && <CopyButton text={t.detail} label={/query/i.test(t.kind) ? "Copy query" : "Copy"} />}
        {t.href && <Link className="small" to={t.href}>{t.hrefLabel ?? "Open"}</Link>}
        {t.external && <a className="small" href={t.external} target="_blank" rel="noreferrer noopener">{t.hrefLabel ?? "Open"} ↗</a>}
      </div>
    </section>
  );
}
