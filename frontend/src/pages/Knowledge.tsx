import { useState, type FormEvent, type ReactNode } from "react";
import { api, qs } from "../api";
import { useAuth } from "../auth";
import { Badge, DataTable, ErrorBox, Loading, PageHead, Stat, Time, useLoad, type Col } from "../components";
import { type Chip, FilterPanel, MultiSelect, useUrlFilters } from "../filters";
import { CopyButton, Drawer, Empty, Identifier, Truncate, useFeedback } from "../ui";

type Tone = "ok" | "warn" | "bad" | "info" | "neutral";

const FRESH: Record<string, Tone> = { current: "ok", stale: "warn", superseded: "warn", deprecated: "warn" };
const INDEXING: Record<string, Tone> = { indexed: "ok", pending: "info", incomplete: "warn", failed: "bad", not_indexed: "neutral" };
const EMBEDDING: Record<string, Tone> = { complete: "ok", partial: "warn", missing: "warn", keyword_only: "neutral", not_applicable: "neutral" };
const words = (s: string) => s.replace(/_/g, " ");
const pct = (v?: number | null) => (v == null ? "—" : `${Math.round(v * 100)}%`);
const shortRev = (r?: string | null) => r?.slice(0, 10) ?? "—";

/** The backend only sends http(s), but this page never makes a link from anything it has not checked itself. */
export const safeHref = (url?: string | null) => (url && /^https?:\/\//i.test(url) ? url : undefined);

function Tally({ counts, tone }: { counts: Record<string, number>; tone: Record<string, Tone> }) {
  const rows = Object.entries(counts ?? {});
  if (!rows.length) return <span className="muted">—</span>;
  return <span className="row tight">{rows.map(([k, n]) => <Badge key={k} kind={tone[k] ?? "neutral"}>{n} {words(k)}</Badge>)}</span>;
}

/** Sync, indexing and embedding finish at different times, so each gets its own answer. */
function SourceProgress({ rows }: { rows: any[] }) {
  if (!rows.length) return null;
  return (
    <details className="card">
      <summary>Source progress ({rows.length})</summary>
      <div className="small muted">A finished sync says nothing about the index. Each column is its own state.</div>
      <DataTable label="Source progress" rows={rows} rowKey={(s) => s.id} cols={[
        { key: "n", head: "Source", primary: true, cell: (s) => <span className="row tight"><Truncate text={s.name} />{s.lifecycle !== "active" && <Badge kind="warn">{s.lifecycle}</Badge>}</span> },
        { key: "s", head: "Sync", cell: (s) => <span className="small">{s.type === "upload" || s.type === "incident" ? <span className="muted">not synced</span> : <>
          <Badge kind={s.sync.status === "ok" ? "ok" : s.sync.status ? "warn" : "neutral"}>{s.sync.status ?? "not synced yet"}</Badge>{s.sync.listing && s.sync.listing !== "complete" && <> <Badge kind="warn">listing {s.sync.listing}</Badge></>}
          {s.sync.remaining > 0 && <span className="muted"> {s.sync.remaining} remaining</span>}{s.sync.failed > 0 && <span className="muted"> {s.sync.failed} failed</span>}
          <div className="muted">last success <Time iso={s.sync.last_success_at} /></div></>}</span> },
        { key: "i", head: "Indexing", cell: (s) => <Tally counts={s.indexing} tone={INDEXING} /> },
        { key: "e", head: "Embedding", cell: (s) => <Tally counts={s.embedding} tone={EMBEDDING} /> },
      ]} />
    </details>
  );
}

const DEFAULTS = { q: "", source: [] as string[], environment: [] as string[], namespace: [] as string[], service: [] as string[], resource_type: [] as string[], kind: [] as string[], owner: [] as string[], team: [] as string[],
  lifecycle: [] as string[], classification: [] as string[], freshness: [] as string[], indexing: [] as string[], embedding: [] as string[], sort: "title", page: 1 };
type FacetKey = Exclude<keyof typeof DEFAULTS, "q" | "sort" | "page">;
const PRIMARY: [FacetKey, string][] = [["source", "Source"], ["environment", "Environment"], ["namespace", "Namespace"], ["freshness", "Freshness"]];
const MORE: [FacetKey, string][] = [["service", "Service"], ["resource_type", "Resource type"], ["kind", "Document type"], ["owner", "Owner"], ["team", "Team"], ["lifecycle", "Lifecycle"], ["classification", "Classification"], ["indexing", "Indexing"], ["embedding", "Embedding"]];
const SIZE = 25;

export function KnowledgeExplorer() {
  const [f, set, clear] = useUrlFilters(DEFAULTS);
  const [text, setText] = useState(f.q);
  const [open, setOpen] = useState<string | null>(null);
  const params: Record<string, string | number> = { q: f.q, sort: f.sort, page: f.page, size: SIZE };
  for (const [k] of [...PRIMARY, ...MORE]) params[k] = f[k].join(",");
  const list = useLoad(() => api<any>(`/knowledge/explorer/documents${qs(params)}`), [JSON.stringify(params)]);
  const d = list.data;
  const facet = ([key, label]: [FacetKey, string]) => {
    const counts: Record<string, number> = d?.facets?.[key] ?? {};
    const options = [...new Set([...Object.keys(counts), ...f[key]])].map((v) => ({ value: v, label: `${words(v)} (${counts[v] ?? 0})` }));
    return <MultiSelect key={key} label={label} options={options} value={f[key]} allLabel="All" onChange={(v) => set({ [key]: v, page: 1 } as any)} />;
  };
  const chips: Chip[] = [...PRIMARY, ...MORE].flatMap(([k, label]) => f[k].map((v) => ({ key: `${k}:${v}`, label: `${label}: ${words(v)}`, onRemove: () => set({ [k]: f[k].filter((x) => x !== v), page: 1 } as any) })));
  if (f.q) chips.unshift({ key: "q", label: `Matches ${f.q}`, onRemove: () => { setText(""); set({ q: "", page: 1 }); } });
  const cols: Col<any>[] = [
    { key: "t", head: "Document", primary: true, cell: (r) => <><span className="row tight nowrap"><Truncate text={r.title} />{r.status === "excluded" ? <Badge kind="warn">excluded</Badge> : (r.lifecycle !== "active" || r.source.lifecycle !== "active") && <Badge kind="warn">{r.lifecycle !== "active" ? r.lifecycle : `source ${r.source.lifecycle}`}</Badge>}</span><Truncate text={r.path} mono /></> },
    { key: "f", head: "Freshness", status: true, cell: (r) => <span title={r.freshness.reason ?? undefined}><Badge kind={FRESH[r.freshness.state]}>{r.freshness.state}</Badge></span> },
    { key: "s", head: "Source", cell: (r) => <span className="small"><Truncate text={r.source.name} /><span className="mono muted">{shortRev(r.revision)}</span></span>, meta: (r) => r.source.name },
    { key: "c", head: "Last checked", cell: (r) => <span className="small"><Time iso={r.checked_at} /></span>, wide: true },
    { key: "o", head: "Owner", cell: (r) => <span className="small">{r.team || r.owner || <span className="muted">no owner</span>}</span>, wide: true },
    { key: "n", head: "Chunks", cell: (r) => r.chunks, wide: true },
    { key: "i", head: "Indexing", cell: (r) => <Badge kind={INDEXING[r.indexing]}>{words(r.indexing)}</Badge> },
    { key: "e", head: "Embedding", cell: (r) => <span className="small">{r.embedding.coverage != null ? pct(r.embedding.coverage) : <Badge kind="neutral">{words(r.embedding.state)}</Badge>}</span>, meta: (r) => words(r.embedding.state) },
  ];
  const pages = d ? Math.max(1, Math.ceil(d.total / SIZE)) : 1;
  return (
    <>
      <PageHead title="Knowledge Explorer" sub="Everything you are allowed to read, from PostgreSQL. Filters and counts only cover those documents." />
      <FilterPanel chips={chips} onClear={() => { setText(""); clear(); }} advanced={<div className="ke-more">{MORE.map(facet)}</div>}
        search={<form className="grow row nowrap" onSubmit={(e) => { e.preventDefault(); set({ q: text.trim(), page: 1 }); }}><input type="search" className="grow" aria-label="Search documents" value={text} onChange={(e) => setText(e.target.value)} placeholder="Title, path or words in the text" /><button className="ghost">Search</button></form>}>
        {PRIMARY.map(facet)}
      </FilterPanel>
      <ErrorBox error={list.error} onRetry={list.reload} />
      {!d && list.loading && <Loading load={list} rows={8} />}
      {d && <>
        <div className="row between small muted mt"><span>{d.total} documents{d.index?.dimensions ? ` · ${d.index.model}, ${d.index.dimensions} dimensions` : d.index ? " · keyword search only, no vectors in this index" : ""}</span>
          <label className="inline">Sort<select aria-label="Sort documents" value={f.sort} onChange={(e) => set({ sort: e.target.value, page: 1 })}><option value="title">Title</option><option value="path">Path</option><option value="checked">Last checked</option><option value="chunks">Chunks</option></select></label></div>
        {d.capped && <div className="alert warn small">Showing the first 20,000 documents, so these counts are partial. Narrow the search.</div>}
        <SourceProgress rows={d.progress} />
        <div className="card mt"><DataTable label="Documents" rows={d.documents} rowKey={(r) => r.id} onRow={(r) => setOpen(r.id)} cols={cols} empty={<Empty title="No documents match.">Only documents you can read are searched. Try removing a filter.</Empty>} /></div>
        {pages > 1 && <div className="row between small mt"><button type="button" className="ghost small" disabled={f.page <= 1} onClick={() => set({ page: f.page - 1 })}>Previous</button><span className="muted">Page {f.page} of {pages}</span><button type="button" className="ghost small" disabled={f.page >= pages} onClick={() => set({ page: f.page + 1 })}>Next</button></div>}
      </>}
      {open && <DocumentDetail id={open} onClose={() => setOpen(null)} onChange={list.reload} onOpen={setOpen} />}
    </>
  );
}

function Section({ title, open = false, children }: { title: string; open?: boolean; children: ReactNode }) {
  return <details className="ke-section" open={open}><summary>{title}</summary><div className="stack">{children}</div></details>;
}

function ChunkBrowser({ id, total }: { id: string; total: number }) {
  const [offset, setOffset] = useState(0);
  const size = 10;
  const page = useLoad(() => api<any>(`/knowledge/explorer/documents/${id}/chunks${qs({ offset, limit: size })}`), [id, offset]);
  if (!total) return <Empty title="No chunks are stored for this document." />;
  return (
    <div className="stack">
      <Loading load={page} rows={3} />
      {page.data?.chunks.map((c: any) => (
        <details key={c.id} className="ke-chunk"><summary><span className="mono">#{c.ord}</span> {c.heading ? <Truncate text={c.heading} /> : <span className="muted">no heading</span>} <span className="small muted">lines {c.lines[0]}–{c.lines[1]} · offsets {c.offsets[0]}–{c.offsets[1]} · about {c.tokens} tokens</span></summary>
          <pre className="passage">{c.text}</pre></details>
      ))}
      {total > size && <div className="row between small"><button type="button" className="ghost small" disabled={!offset} onClick={() => setOffset(Math.max(0, offset - size))}>Previous</button><span className="muted">{offset + 1}–{Math.min(offset + size, total)} of {total}</span><button type="button" className="ghost small" disabled={offset + size >= total} onClick={() => setOffset(offset + size)}>Next</button></div>}
    </div>
  );
}

function DocumentDetail({ id, onClose, onChange, onOpen }: { id: string; onClose: () => void; onChange: () => void; onOpen: (id: string) => void }) {
  const { toast, confirm } = useFeedback();
  const doc = useLoad(() => api<any>(`/knowledge/explorer/documents/${id}`), [id]);
  const d = doc.data;
  const done = () => { doc.reload(); onChange(); };
  const act = (path: string, method: string, body: any, ok: string) => api(path, { method, body }).then(() => { toast(ok); done(); }, (e) => toast(e.message, "bad"));
  async function move(state: string, verb: string) {
    if (await confirm({ title: `${verb} this document?`, body: state === "active" ? "It becomes searchable at once." : "Search stops returning it at once. The text is kept.", action: verb, danger: state !== "active" })) act(`/knowledge/documents/${id}/lifecycle`, "PUT", { state, reason: "from the Knowledge Explorer" }, `${verb}d`);
  }
  const url = safeHref(d?.url);
  return (
    <Drawer title={d?.title ?? "Document"} onClose={onClose}>
      <ErrorBox error={doc.error} />
      {!d && doc.loading && <Loading load={doc} rows={6} />}
      {d && <div className="stack">
        <Identifier name={d.path} what="path" />
        <div className="row small">
          <Badge kind={FRESH[d.freshness.state]}>{d.freshness.state}</Badge><Badge kind={d.lifecycle === "active" ? "neutral" : "warn"}>{d.lifecycle}</Badge><Badge kind={d.trust === "reviewed" ? "ok" : "neutral"}>{d.trust}</Badge>
          <Badge kind={INDEXING[d.indexing]}>{words(d.indexing)}</Badge><Badge kind={EMBEDDING[d.embedding.state]}>{words(d.embedding.state)}</Badge>
          {d.status !== "active" && <Badge kind="warn">{d.status}</Badge>}
        </div>
        {d.freshness.reason && <div className="alert warn small">{d.freshness.reason}</div>}
        {d.excluded_reason && <div className="alert warn small">Excluded: {d.excluded_reason}. Its content was not stored, so there is nothing to show.</div>}
        {d.note && <div className="alert warn small">{d.note}</div>}
        {d.superseded_by && <div className="alert info small">Replaced by {d.superseded_by.id ? <button type="button" className="link" onClick={() => onOpen(d.superseded_by.id)}>{d.superseded_by.title}</button> : <span className="mono">{d.superseded_by.path}</span>}.</div>}
        {d.replaces.length > 0 && <div className="small muted">Replaces: {d.replaces.map((o: any) => <button key={o.id} type="button" className="link" onClick={() => onOpen(o.id)}>{o.title || o.path}</button>)}</div>}
        {(d.can_review || d.can_operate) && <div className="row small">
          {d.can_review && d.trust !== "reviewed" && d.status === "active" && <button type="button" className="ghost small" onClick={() => act(`/knowledge/documents/${id}/review`, "POST", {}, "Marked reviewed")}>Mark reviewed</button>}
          {d.can_review && d.lifecycle === "active" && <button type="button" className="ghost small" onClick={() => move("quarantined", "Quarantine")}>Quarantine</button>}
          {d.can_review && d.lifecycle === "active" && <button type="button" className="ghost small" onClick={() => move("archived", "Archive")}>Archive</button>}
          {d.can_review && ["draft", "quarantined", "archived"].includes(d.lifecycle) && <button type="button" className="ghost small" onClick={() => move("active", "Activate")}>Make active</button>}
          {d.can_operate && d.status === "active" && d.indexing !== "indexed" && <button type="button" className="ghost small" onClick={() => act(`/knowledge/explorer/documents/${id}/retry`, "POST", {}, "Queued for the index")}>Retry indexing</button>}
        </div>}
        <dl className="kv small">
          <dt>Source</dt><dd>{d.source.name} · {d.source.type}</dd><dt>Scope</dt><dd className="mono">{d.environment}/{d.namespace}</dd><dt>Owner</dt><dd>{d.team || d.owner || "none"}</dd>
          <dt>Classification</dt><dd>{d.classification}{d.review.shared_globally ? ", shared globally" : ""}</dd><dt>Service</dt><dd>{d.service || "—"}</dd><dt>Last checked</dt><dd><Time iso={d.checked_at} /></dd>
          <dt>Next review</dt><dd>{d.review.review_due_at ? new Date(d.review.review_due_at).toLocaleDateString() : d.review.source_review_interval_days ? `every ${d.review.source_review_interval_days} days` : "not set"}</dd>
        </dl>
        {url && <a href={url} target="_blank" rel="noreferrer noopener" className="small">Open the original</a>}
        <Section title="Preview" open>
          <div className="small muted">Sanitized text as MOKS holds it. Documented guidance, not a current fact.</div>
          {d.preview ? <pre className="logbox ke-preview">{d.preview}</pre> : <Empty title="No text is stored for this document." />}
          {d.preview_cut && <div className="small muted">Showing the start of this document. The chunks below have the rest.</div>}
        </Section>
        <Section title={`Chunks (${d.chunks})`}><ChunkBrowser id={id} total={d.chunks} /></Section>
        <Section title="Version and citation">
          <dl className="kv small">
            <dt>Revision</dt><dd className="mono">{d.citation.revision ?? "—"}</dd><dt>Content last changed in</dt><dd className="mono">{d.citation.content_changed_in ?? "—"}</dd>
            <dt>Pipeline</dt><dd>v{d.citation.pipeline_version}{d.citation.pipeline_version !== d.citation.current_pipeline ? ` (current is v${d.citation.current_pipeline})` : ""}</dd>
            <dt>Content revision</dt><dd>{d.citation.content_revision}, indexed {d.citation.indexed_revision}</dd><dt>Size</dt><dd>{d.citation.size_bytes.toLocaleString()} bytes</dd>
          </dl>
          <div className="row small"><span className="mono muted trunc" title={d.citation.content_hash}>{d.citation.content_hash}</span><CopyButton text={d.citation.content_hash} label="Copy hash" /></div>
        </Section>
        <Section title="Index membership">
          <div className="small">Embedding coverage: <strong>{d.embedding.coverage != null ? `${d.embedding.vectors} of ${d.embedding.expected} chunks (${pct(d.embedding.coverage)})` : words(d.embedding.state)}</strong>. Vectors themselves are not shown.</div>
          {d.generations.length ? <DataTable label="Index generations" rows={d.generations} rowKey={(g: any) => `${g.status}${g.model}${g.revision}`} cols={[
            { key: "s", head: "Generation", primary: true, cell: (g: any) => <span className="small">{g.index ? <Truncate text={g.index} mono /> : null}<Badge kind={g.serving ? "ok" : "neutral"}>{g.serving ? "serving" : g.status}</Badge></span> },
            { key: "m", head: "Model", cell: (g: any) => <span className="small">{g.model ?? "—"}{g.dimensions ? `, ${g.dimensions} dimensions` : ""}</span> },
            { key: "c", head: "Chunks", cell: (g: any) => `${g.chunks} of ${g.expected_chunks ?? "?"}` },
            { key: "v", head: "Vectors", cell: (g: any) => `${g.vectors} of ${g.expected_vectors ?? "?"}` },
            { key: "r", head: "Revision", cell: (g: any) => <Badge kind={g.current ? "ok" : "warn"}>{g.current ? "current" : `behind (${g.revision})`}</Badge> },
          ]} /> : <div className="small muted">No index generation has recorded this document.</div>}
        </Section>
        {d.operations && <Section title="Retries and errors">
          <div className="small">Vector state: {d.operations.vector_state}{d.operations.vector_attempts ? `, ${d.operations.vector_attempts} failed tries` : ""}{d.operations.vector_error ? ` · ${d.operations.vector_error}` : ""}</div>
          {d.operations.events.map((e: any) => <div key={e.id} className="small"><Badge kind={e.state === "dead" ? "bad" : "warn"}>{e.state}</Badge> {e.op} revision {e.revision} · {e.attempts} tries · {e.error}</div>)}
          {d.operations.generation_errors.map((e: any, i: number) => <div key={i} className="small"><Badge kind="warn">{e.status}</Badge> {e.retries} retries · {e.error}</div>)}
          {!d.operations.events.length && !d.operations.generation_errors.length && !d.operations.vector_error && <div className="small muted">Nothing is waiting or failing.</div>}
        </Section>}
      </div>}
    </Drawer>
  );
}

// ---- retrieval test workspace ----------------------------------------------------------------------

const MODES = ["keyword", "vector", "hybrid"];
const VERDICTS: [string, string][] = [["relevant", "Relevant"], ["irrelevant", "Irrelevant"], ["outdated", "Outdated"]];

function Hit({ h, rank, onFeedback }: { h: any; rank: number; onFeedback: (verdict: string, h: any) => void }) {
  const href = safeHref(h.citation.url);
  const signals = Object.entries(h.signals ?? {}) as [string, number][];
  return (
    <div className="ke-hit">
      <div className="row between nowrap"><strong className="trunc" title={h.title}>{rank}. {h.title}{h.heading && h.heading !== h.title && <span className="muted"> › {h.heading}</span>}</strong><Badge kind={FRESH[h.freshness.state]}>{h.freshness.state}</Badge></div>
      <div className="small">{href ? <a href={href} target="_blank" rel="noreferrer noopener"><Truncate text={`${h.citation.path} L${h.citation.lines?.[0]}-${h.citation.lines?.[1]}`} mono /></a> : <Truncate text={h.citation.path} mono />}
        <span className="muted"> {h.citation.source} · {h.scope.environment}/{h.scope.namespace}{h.citation.revision ? ` · ${h.citation.revision.slice(0, 10)}` : ""}</span></div>
      {h.freshness.reason && <div className="small muted">{h.freshness.reason}</div>}
      {h.freshness.possible_conflict && <div className="small"><Badge kind="warn">may conflict</Badge> <span className="muted">with {h.freshness.conflict_with?.join(", ")}. MOKS does not decide which is right.</span></div>}
      <details><summary className="small">Passage and ranking signals</summary>
        <pre className="passage">{h.text}</pre>
        <div className="small muted">Rank score {h.score}. This orders one result list and is not a probability.{signals.length > 0 && <> Signals: {signals.map(([k, v]) => `${words(k)} ${v > 0 ? "+" : ""}${v}`).join(", ")}.</>}{h.ranking && <> Moved from {h.ranking.before} to {h.ranking.after}{h.ranking.pinned ? ", pinned by an exact identifier" : ""}.</>}</div>
      </details>
      <div className="row tight small" role="group" aria-label={`Feedback on ${h.title}`}>{VERDICTS.map(([v, label]) => <button key={v} type="button" className="ghost small" onClick={() => onFeedback(v, h)}>{label}</button>)}</div>
    </div>
  );
}

function Variant({ v, onFeedback, onMissing }: { v: any; onFeedback: (verdict: string, h: any, v: any) => void; onMissing: (v: any) => void }) {
  return (
    <div className="card stack ke-variant">
      <div className="row between"><h3>{words(v.name)}</h3><span className="row tight"><Badge kind={v.mode === "none" ? "warn" : "info"}>ran: {v.mode}</Badge>{v.abstain ? <Badge kind="warn">abstains</Badge> : <Badge kind="ok">has passages</Badge>}</span></div>
      <div className="small muted">{v.latency_ms != null ? `${v.latency_ms} ms` : ""}{v.reranking ? ` · reranking ${v.reranking.state}` : ""}</div>
      {v.fallback_reason && <div className="alert warn small">{v.fallback_reason}</div>}
      {v.note && <div className="alert info small">{v.note}</div>}
      <div className="small">{v.evidence.reason}{v.evidence.missing_identifiers?.length > 0 && <> Not found: <span className="mono">{v.evidence.missing_identifiers.join(", ")}</span>.</>}</div>
      {v.results.map((h: any, i: number) => <Hit key={h.chunk_id} h={h} rank={i + 1} onFeedback={(verdict, hit) => onFeedback(verdict, hit, v)} />)}
      {!v.results.length && <Empty title="No passages." />}
      {v.diagnostics && <details className="small"><summary>Diagnostics</summary><pre className="passage">{JSON.stringify(v.diagnostics, null, 2)}</pre></details>}
      <button type="button" className="ghost small" onClick={() => onMissing(v)}>Report missing information</button>
    </div>
  );
}

export function RetrievalWorkspace() {
  const { toast } = useFeedback();
  const scope = useLoad(() => api<any>(`/knowledge/explorer/documents${qs({ size: 1 })}`), []);
  const [f, setF] = useState({ query: "", environment: "", namespace: "", sources: [] as string[], modes: MODES, k: 8, claim: "" });
  const [res, setRes] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const facets = scope.data?.facets;
  async function run(e: FormEvent) {
    e.preventDefault();
    setBusy(true); setError(null);
    try { setRes(await api("/knowledge/retrieval/test", { method: "POST", body: { query: f.query, environment: f.environment || null, namespace: f.namespace || null, sources: f.sources, modes: f.modes, k: f.k, claim: f.claim || null } })); }
    catch (err) { setError(err); } finally { setBusy(false); }
  }
  const send = (verdict: string, chunk: string | null, v: any) => api("/knowledge/feedback", { method: "POST", body: { query: f.query, verdict, chunk_id: chunk, environment: f.environment || null, namespace: f.namespace || null, mode: v.mode, reranked: v.reranked } })
    .then(() => toast("Saved for review. It changes nothing by itself."), (e) => toast(e.message, "bad"));
  return (
    <>
      <PageHead title="Retrieval test" sub="Compare how search answers one question. It searches only what you can read, and changes nothing." />
      <form className="card stack" onSubmit={run}>
        <label>Question<textarea required minLength={2} maxLength={500} rows={2} value={f.query} onChange={(e) => setF({ ...f, query: e.target.value })} placeholder="What does ERR_PAY_4071 mean? How do I fail over redis?" /></label>
        <div className="row">
          <label>Environment<select value={f.environment} onChange={(e) => setF({ ...f, environment: e.target.value })}><option value="">All I can read</option>{Object.keys(facets?.environment ?? {}).filter((x) => x !== "*").map((x) => <option key={x}>{x}</option>)}</select></label>
          <label>Namespace<select value={f.namespace} onChange={(e) => setF({ ...f, namespace: e.target.value })}><option value="">All I can read</option>{Object.keys(facets?.namespace ?? {}).filter((x) => x !== "*").map((x) => <option key={x}>{x}</option>)}</select></label>
          <MultiSelect label="Sources" allLabel="All sources" value={f.sources} options={(scope.data?.progress ?? []).map((s: any) => ({ value: s.id, label: s.name }))} onChange={(sources) => setF({ ...f, sources })} />
          <label>Passages<input type="number" min={1} max={20} value={f.k} onChange={(e) => setF({ ...f, k: Number(e.target.value) })} /></label>
        </div>
        <fieldset className="row"><legend className="small">Compare</legend>{MODES.map((m) => <label key={m} className="inline"><input type="checkbox" checked={f.modes.includes(m)} disabled={f.modes.length === 1 && f.modes[0] === m} onChange={(e) => setF({ ...f, modes: e.target.checked ? [...f.modes, m] : f.modes.filter((x) => x !== m) })} /> {m}</label>)}</fieldset>
        <details className="small"><summary>Check a claim against the passages</summary><label>Claim<input maxLength={500} value={f.claim} onChange={(e) => setF({ ...f, claim: e.target.value })} placeholder="A sentence you expect the passages to state" /></label></details>
        <ErrorBox error={error} />
        <div><button disabled={busy || f.query.length < 2}>{busy ? "Searching…" : "Compare"}</button></div>
      </form>
      {res && <div className="stack mt">
        <div className="alert info small">{res.note}</div>
        <div className="stat-row">
          <Stat value={res.usage.embedding_calls} label="embedding calls" />
          <Stat value={res.usage.tokens} label="tokens, estimated" />
          <Stat value={res.reranker.configured ? "on" : "off"} label="reranker" />
        </div>
        {res.reranker.note && <div className="small muted">{res.reranker.note}</div>}
        {res.claim && <div className="card small"><Badge kind={res.claim.state === "supported" ? "ok" : "warn"}>{res.claim.state}</Badge> {res.claim.text} <span className="muted">{res.claim.basis}, {Math.round(res.claim.overlap * 100)}% of its terms in one passage. A citation existing does not prove a claim.</span></div>}
        <div className="ke-variants">{res.variants.map((v: any) => <Variant key={v.name} v={v} onFeedback={(verdict, h, variant) => send(verdict, h.chunk_id, variant)} onMissing={(variant) => send("missing", null, variant)} />)}</div>
      </div>}
    </>
  );
}

// ---- review ---------------------------------------------------------------------------------------

export function KnowledgeReview() {
  const { can } = useAuth();
  const { toast } = useFeedback();
  const reviewer = can("knowledge.review");
  const proposals = useLoad(() => api<any[]>("/proposals"), []);
  const feedback = useLoad(() => (reviewer ? api<any[]>("/knowledge/feedback") : Promise.resolve([])), [reviewer]);
  const docs = useLoad(() => (reviewer ? api<any>(`/knowledge/explorer/documents${qs({ lifecycle: "draft,quarantined", size: 50 })}`) : Promise.resolve(null)), [reviewer]);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const decide = (p: any, decision: string) => api(`/proposals/${p.id}/review`, { method: "POST", body: { decision, note: notes[p.id] ?? "" } }).then((r) => { toast(decision === "approved" ? (r.draft?.kind === "skill" ? "Registered as an unpublished version. It runs nowhere until it is reviewed and published." : "Saved as a draft document. Nobody can retrieve it until it is made active.") : "Rejected"); proposals.reload(); docs.reload(); }, (e) => toast(e.message, "bad"));
  const close = (id: string, state: string) => api(`/knowledge/feedback/${id}`, { method: "PUT", body: { state } }).then(feedback.reload, (e) => toast(e.message, "bad"));
  const open = (proposals.data ?? []).filter((p) => p.state === "draft");
  return (
    <>
      <PageHead title="Knowledge review" sub="Proposals from incidents, documents waiting for a decision, and what people said about search results." />
      <div className="card stack">
        <h2>Proposals from incidents ({open.length})</h2>
        <Loading load={proposals} rows={3} />
        {proposals.data && !open.length && <Empty title="Nothing is waiting.">Engineers propose changes from a finished investigation.</Empty>}
        {open.map((p) => <details key={p.id} className="ke-section"><summary><Badge>{p.kind}</Badge> {p.title} <span className="small muted">v{p.version} · {p.owner}</span></summary>
          <div className="stack"><pre className="passage">{p.body}</pre>
            <div className="small muted">Evidence {p.evidence.join(", ")} · regression question: {p.regression_fixture?.question}</div>
            <label>Review note<input minLength={3} value={notes[p.id] ?? ""} onChange={(e) => setNotes({ ...notes, [p.id]: e.target.value })} /></label>
            <div className="small muted">Approving makes a draft. It publishes nothing, and someone must still publish it.</div>
            <div className="row"><button type="button" onClick={() => decide(p, "approved")} disabled={(notes[p.id] ?? "").length < 3}>Approve as a draft</button><button type="button" className="ghost" onClick={() => decide(p, "rejected")} disabled={(notes[p.id] ?? "").length < 3}>Reject</button></div></div>
        </details>)}
      </div>
      {reviewer && <div className="card stack mt"><h2>Documents waiting ({docs.data?.total ?? 0})</h2><Loading load={docs} rows={3} />
        {docs.data && <DataTable label="Documents waiting" rows={docs.data.documents} rowKey={(d: any) => d.id} empty={<Empty title="Nothing is waiting." />} cols={[
          { key: "t", head: "Document", primary: true, cell: (d: any) => <><Truncate text={d.title} /><Truncate text={d.path} mono /></> },
          { key: "l", head: "Lifecycle", status: true, cell: (d: any) => <Badge kind="warn">{d.lifecycle}</Badge> },
          { key: "s", head: "Scope", cell: (d: any) => <span className="mono small">{d.environment}/{d.namespace}</span> },
        ]} />}<div className="small muted">Open these in the Knowledge Explorer to read them and decide.</div></div>}
      {reviewer && <div className="card stack mt"><h2>Search feedback ({feedback.data?.length ?? 0})</h2>
        <div className="small muted">Review input. Nothing here retrains a model or changes ranking by itself.</div>
        <Loading load={feedback} rows={3} />
        {feedback.data && <DataTable label="Search feedback" rows={feedback.data} rowKey={(f: any) => f.id} empty={<Empty title="No open feedback." />} menu={(f: any) => ({ label: `Actions for feedback on ${f.query}`, items: [{ label: "Mark reviewed", run: () => close(f.id, "reviewed") }, { label: "Dismiss", run: () => close(f.id, "dismissed") }] })} cols={[
          { key: "q", head: "Question", primary: true, cell: (f: any) => <Truncate text={f.query} /> },
          { key: "v", head: "Verdict", status: true, cell: (f: any) => <Badge kind={f.verdict === "relevant" ? "ok" : "warn"}>{f.verdict}</Badge> },
          { key: "n", head: "Note", cell: (f: any) => <span className="small">{f.note || "—"}</span> },
          { key: "c", head: "When", cell: (f: any) => <span className="small"><Time iso={f.created_at} /></span> },
        ]} />}</div>}
    </>
  );
}
