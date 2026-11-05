import { useState, type FormEvent } from "react";
import { api, API, qs } from "../api";
import { useAuth } from "../auth";
import { Badge, DataTable, EnvNsFilter, ErrorBox, Loading, PageHead, Time, useLoad, useScopes, type Col } from "../components";
import { Drawer, Empty, useFeedback } from "../ui";

const MODE_TONE: Record<string, "ok" | "info" | "neutral"> = { enabled: "ok", shadow: "info", disabled: "neutral" };
const STATUS: Record<string, [("ok" | "warn" | "bad" | "info" | "neutral"), string]> = {
  insufficient_history: ["neutral", "insufficient history"], no_telemetry: ["warn", "no telemetry"], maintenance: ["info", "maintenance"], model_invalid: ["bad", "model invalid"],
};
const FEATURE: Record<string, string> = {
  request_rate: "request rate", error_ratio: "error ratio", latency_p95: "p95 latency", cpu_throttling: "CPU throttling", memory_utilization: "memory use of limit", restart_rate: "restarts per hour",
};

export function observationLabel(o: any): [("ok" | "warn" | "bad" | "info" | "neutral"), string] {
  if (o.status === "scored") return o.unusual ? ["warn", "unusual"] : ["ok", "typical"];
  return STATUS[o.status] ?? ["neutral", o.status];
}

function Modes({ caps }: { caps?: Record<string, any> }) {
  if (!caps) return null;
  return <div className="row wrap" aria-label="Capability states">
    {Object.entries(caps).map(([name, c]) => <div key={name} className="card tight small">
      <div className="row tight"><strong>{name.replace(/_/g, " ")}</strong><Badge kind={MODE_TONE[c.mode] ?? "neutral"}>{c.mode}</Badge>{c.forced_off && <Badge kind="warn">forced off</Badge>}</div>
      <div className="muted">{c.describe}</div>
    </div>)}
  </div>;
}

function Quality({ q }: { q: any }) {
  if (!q) return null;
  const absent: string[] = q.absent ?? [];
  const low = Object.entries(q.coverage ?? {}).filter(([f, v]: any) => v < 0.5 && !absent.includes(f)).map(([f]) => f);
  return <ul className="small">
    {absent.length > 0 && <li>No telemetry for {absent.map((f) => FEATURE[f] ?? f).join(", ")}. Absent data is not treated as healthy.</li>}
    {low.length > 0 && <li>Under half coverage: {low.map((f) => FEATURE[f] ?? f).join(", ")}.</li>}
    {q.sparse_traffic_points > 0 && <li>{q.sparse_traffic_points} point(s) with too little traffic to judge an error ratio.</li>}
    {q.counter_resets > 0 && <li>{q.counter_resets} counter reset(s) handled.</li>}
    {q.incomplete_queries?.length > 0 && <li>Incomplete queries: {q.incomplete_queries.join(", ")}.</li>}
    {q.error && <li>Model not used: {q.error}</li>}
    {q.drift_psi && Object.keys(q.drift_psi).length > 0 && <li>Distribution change against training (PSI): {Object.entries(q.drift_psi).map(([f, v]) => `${FEATURE[f] ?? f} ${v}`).join(", ")}.</li>}
  </ul>;
}

function ObservationDrawer({ o, onClose, onSaved }: { o: any; onClose: () => void; onSaved: (o: any) => void }) {
  const { can } = useAuth();
  const { toast } = useFeedback();
  const [tone, label] = observationLabel(o);
  const send = async (verdict: string) => {
    try {
      onSaved(await api(`/ml/observations/${o.id}/feedback`, { method: "POST", body: { verdict } }));
      toast("Recorded for review.");
    } catch (e: any) {
      toast(e?.message ?? "Could not record that", "bad");
    }
  };
  return <Drawer title="Observation" heading={<span className="row tight"><span className="mono">{o.cohort}</span><Badge kind={tone}>{label}</Badge></span>} onClose={onClose}
    footer={can("ml.feedback") && o.status === "scored" ? <div className="row wrap" role="group" aria-label="Was this unusual behavior real?">
      {[["true_positive", "Real issue"], ["expected", "Expected change"], ["false_positive", "False alarm"], ["unsure", "Unsure"]].map(([v, l]) =>
        <button key={v} type="button" className={`ghost${o.feedback?.verdict === v ? " selected" : ""}`} aria-pressed={o.feedback?.verdict === v} onClick={() => send(v)}>{l}</button>)}
    </div> : undefined}>
    <div className="stack">
      <div className="alert info small">{o.context?.note}</div>
      <div className="small">Window <Time iso={o.window_from} /> to <Time iso={o.window_to} /> · {o.algorithm.replace("_", " ")} v{o.context?.model_version} ({o.context?.role}) · mode {o.mode}</div>
      {o.score !== null && <div className="small">Score {o.score} against threshold {o.threshold}. A score ranks how unusual the window is; it is not a probability of a fault.</div>}
      {o.context?.maintenance && <div className="alert warn small">Maintenance window: {o.context.maintenance.reason}. Scored for the record, never flagged.</div>}
      {o.deviations?.length > 0 && <div className="table-wrap" role="region" aria-label="Feature deviations" tabIndex={0}><table>
        <thead><tr><th scope="col">Feature</th><th scope="col">Observed</th><th scope="col">Typical here</th><th scope="col">Deviation</th></tr></thead>
        <tbody>{o.deviations.map((d: any) => <tr key={d.feature}><td>{FEATURE[d.feature] ?? d.feature}</td><td className="mono">{d.observed}</td><td className="mono">{d.typical}</td><td>{d.z > 0 ? "+" : ""}{d.z} ({d.direction})</td></tr>)}</tbody>
      </table></div>}
      <div className="small muted">Deviations say which features moved and how far, against this workload's own history. They do not say why.</div>
      <Quality q={o.data_quality} />
    </div>
  </Drawer>;
}

/** Shadow-mode observations and the state of each ML capability, for on-call engineers. */
export function MLInsights() {
  const scopes = useScopes();
  const [env, setEnv] = useState("all");
  const [ns, setNs] = useState("all");
  const [unusual, setUnusual] = useState(false);
  const [open, setOpen] = useState<any>(null);
  const status = useLoad(() => api("/ml/status"), []);
  const obs = useLoad(() => api(`/ml/observations${qs({ environment: env === "all" ? undefined : env, namespace: ns === "all" ? undefined : ns, unusual: unusual || undefined })}`), [env, ns, unusual]);
  const caps = status.data?.capabilities;
  const cols: Col<any>[] = [
    { key: "cohort", head: "Workload", primary: true, cell: (o) => <span className="mono">{o.cohort}</span> },
    { key: "status", head: "Status", status: true, cell: (o) => { const [t, l] = observationLabel(o); return <Badge kind={t}>{l}</Badge>; } },
    { key: "top", head: "Moved most", cell: (o) => o.deviations?.[0] ? `${FEATURE[o.deviations[0].feature] ?? o.deviations[0].feature} ${o.deviations[0].direction}` : "" },
    { key: "model", head: "Model", cell: (o) => `${o.algorithm.replace("_", " ")} v${o.context?.model_version ?? "?"}${o.context?.role === "challenger" ? " (challenger)" : ""}` },
    { key: "when", head: "Window end", cell: (o) => <Time iso={o.window_to} /> },
    { key: "fb", head: "Feedback", cell: (o) => o.feedback?.verdict?.replace("_", " ") ?? "" },
  ];
  return <div className="stack">
    <PageHead title="ML insights" sub="Advisory signals. They never open incidents, change severity, grant access or run anything." />
    {status.error ? <ErrorBox error={status.error} /> : <Modes caps={caps} />}
    <div className="filters row wrap">
      <EnvNsFilter scopes={scopes.data} env={env} ns={ns} onEnv={setEnv} onNs={setNs} />
      <label className="check"><input type="checkbox" checked={unusual} onChange={(e) => setUnusual(e.target.checked)} /> Unusual only</label>
    </div>
    <h2>Anomaly observations</h2>
    {obs.data ? <DataTable label="Anomaly observations" rows={obs.data.observations} cols={cols} rowKey={(o) => o.id} onRow={setOpen}
      empty={<Empty title={caps?.anomaly_detection?.mode === "disabled" ? "Anomaly detection is off." : "No observations in the last 24 hours."}>
        {caps?.anomaly_detection?.mode === "disabled" ? "An ML administrator can switch it to shadow mode after training and promoting a model."
          : "Workloads appear here once a champion model exists for them. Insufficient history and missing telemetry are listed, not hidden."}</Empty>} />
      : <Loading load={obs} />}
    <div className="small muted">{obs.data?.note}</div>
    <div className="small muted">Similar past incidents and skill recommendations appear inside each investigation, where they can be judged.</div>
    {open && <ObservationDrawer o={open} onClose={() => setOpen(null)} onSaved={(o) => { setOpen(o); obs.reload(); }} />}
  </div>;
}

function CapabilityForm({ name, c, onSaved }: { name: string; c: any; onSaved: () => void }) {
  const { toast } = useFeedback();
  const [mode, setMode] = useState(c.mode);
  const [reason, setReason] = useState("");
  const save = async (e: FormEvent) => {
    e.preventDefault();
    try {
      await api(`/ml/capabilities/${name}`, { method: "PUT", body: { mode, reason } });
      toast(`${name} is now ${mode}.`);
      setReason("");
      onSaved();
    } catch (err: any) {
      toast(err?.message ?? "Could not change the mode", "bad");
    }
  };
  return <form className="card tight stack" onSubmit={save} aria-label={`${name} mode`}>
    <div className="row tight"><strong>{name.replace(/_/g, " ")}</strong><Badge kind={MODE_TONE[c.mode] ?? "neutral"}>{c.mode}</Badge></div>
    <div className="small muted">{c.describe}</div>
    <div className="row wrap">
      <label>Mode<select value={mode} onChange={(e) => setMode(e.target.value)}>{c.allowed.map((m: string) => <option key={m} value={m}>{m}</option>)}</select></label>
      <label className="grow">Reason (audited)<input value={reason} onChange={(e) => setReason(e.target.value)} minLength={3} required /></label>
      <button type="submit" disabled={c.forced_off || mode === c.mode}>Save</button>
    </div>
    {c.forced_off && <div className="small muted">MOKS_ML_DISABLED is set for this deployment: every capability is off whatever is saved here.</div>}
  </form>;
}

function Models() {
  const { toast, confirm } = useFeedback();
  const models = useLoad(() => api<any[]>("/ml/models"), []);
  const act = async (path: string, body: any, done: string) => {
    try {
      await api(path, { method: "POST", body });
      toast(done);
      models.reload();
    } catch (e: any) {
      toast(e?.message ?? "Refused", "bad");
    }
  };
  const cols: Col<any>[] = [
    { key: "cohort", head: "Cohort", primary: true, cell: (m) => <span className="mono">{m.cohort}</span> },
    { key: "alg", head: "Algorithm", cell: (m) => `${m.algorithm.replace("_", " ")} v${m.version}` },
    { key: "status", head: "Status", status: true, cell: (m) => <Badge kind={m.status === "champion" ? "ok" : m.status === "shadow" ? "info" : m.status === "rejected" ? "bad" : "neutral"}>{m.status}</Badge> },
    { key: "gates", head: "Gates", cell: (m) => m.gates_passed ? <Badge kind="ok">passed</Badge> : <Badge kind="warn">{m.gates_failed.join(", ") || "none"}</Badge> },
    { key: "flags", head: "Flags/day (validation)", cell: (m) => m.metrics?.validation?.flags_per_day ?? "" },
    { key: "vs", head: "Champion flags/day", cell: (m) => m.metrics?.champion_comparison?.champion_flags_per_day ?? "" },
    { key: "sha", head: "Artifact", wide: true, cell: (m) => <span className="mono small">{m.artifact_sha256?.slice(0, 12)}</span> },
    { key: "at", head: "Trained", cell: (m) => <Time iso={m.created_at} /> },
  ];
  return <>
    <h2>Models</h2>
    {models.data ? <DataTable label="Models" rows={models.data} cols={cols} rowKey={(m) => m.id} empty={<Empty title="No models yet.">Train a namespace below. Training registers candidates and promotes nothing.</Empty>}
      menu={(m) => ({ label: `Actions for ${m.cohort} v${m.version}`, items: [
        { label: "Run as challenger (shadow)", disabled: m.status === "shadow" || m.status === "champion", run: () => act(`/ml/models/${m.id}/promote`, { to: "shadow" }, "Running as challenger.") },
        { label: "Make champion", disabled: !m.gates_passed || m.status === "champion", run: () => act(`/ml/models/${m.id}/promote`, { to: "champion" }, "Champion changed. The previous one is kept for rollback.") },
        { label: "Roll back this cohort", disabled: m.status !== "champion", run: async () => { if (await confirm({ title: "Roll back", body: "The champion steps down and the previously promoted model returns. With none, this cohort shows no observations.", action: "Roll back" })) act("/ml/rollback", { capability: m.capability, cohort: m.cohort }, "Rolled back."); } },
        { label: "Reject", danger: true, disabled: m.status === "rejected", run: () => act(`/ml/models/${m.id}/reject`, { note: "rejected by an ML administrator" }, "Rejected.") },
      ] })} /> : <Loading load={models} />}
  </>;
}

function Train() {
  const { toast } = useFeedback();
  const [environment, setEnv] = useState("");
  const [namespace, setNs] = useState("");
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    try {
      const r = await api("/ml/train", { method: "POST", body: { environment, namespace } });
      toast(r.queued ? "Training queued. Candidates appear above when it finishes." : "A training run for this namespace is already queued.");
    } catch (err: any) {
      toast(err?.message ?? "Could not queue training", "bad");
    }
  };
  return <form className="row wrap" onSubmit={submit} aria-label="Train anomaly models">
    <label>Environment<input value={environment} onChange={(e) => setEnv(e.target.value)} required pattern="[a-z0-9-]+" /></label>
    <label>Namespace<input value={namespace} onChange={(e) => setNs(e.target.value)} required pattern="[a-z0-9]([-a-z0-9]*[a-z0-9])?" /></label>
    <button type="submit">Train</button>
  </form>;
}

function Review() {
  const { toast } = useFeedback();
  const recs = useLoad(() => api<any[]>("/ml/learning/records?label_state=unreviewed&kind=investigation&limit=50"), []);
  const [open, setOpen] = useState<any>(null);
  const [form, setForm] = useState({ confirmed_cause: "", resolution: "", verification: "unverified" });
  const submit = async (approve: boolean) => {
    try {
      await api(`/ml/learning/records/${open.id}/review`, { method: "POST", body: { approve, ...form } });
      toast(approve ? "Approved: this record can enter a dataset." : "Rejected.");
      setOpen(null);
      recs.reload();
    } catch (e: any) {
      toast(e?.message ?? "Could not save the review", "bad");
    }
  };
  const cols: Col<any>[] = [
    { key: "scope", head: "Scope", primary: true, cell: (r) => <span className="mono">{r.environment}/{r.namespace}{r.service ? `/${r.service}` : ""}</span> },
    { key: "sym", head: "Symptoms", cell: (r) => (r.features?.symptoms ?? []).join(", ") },
    { key: "out", head: "Outcome", cell: (r) => `investigation ${r.outcome?.investigation_status}; resolution ${r.outcome?.resolution}` },
    { key: "at", head: "When", cell: (r) => <Time iso={r.created_at} /> },
  ];
  return <>
    <h2>Confirm outcomes</h2>
    <div className="small muted">A completed investigation is not a resolved incident. Only what you confirm here becomes a label.</div>
    {recs.data ? <DataTable label="Unreviewed learning records" rows={recs.data} cols={cols} rowKey={(r) => r.id} onRow={setOpen} empty={<Empty title="Nothing waiting for review." />} /> : <Loading load={recs} />}
    {open && <Drawer title="Confirm what happened" heading={<span className="mono">{open.environment}/{open.namespace}</span>} onClose={() => setOpen(null)}
      footer={<div className="row wrap"><button type="button" onClick={() => submit(true)} disabled={!form.confirmed_cause.trim()}>Approve</button><button type="button" className="ghost" onClick={() => submit(false)}>Reject</button></div>}>
      <div className="stack">
        <label>Confirmed cause<textarea value={form.confirmed_cause} onChange={(e) => setForm({ ...form, confirmed_cause: e.target.value })} rows={3} /></label>
        <label>Resolution<textarea value={form.resolution} onChange={(e) => setForm({ ...form, resolution: e.target.value })} rows={2} /></label>
        <label>Verification<select value={form.verification} onChange={(e) => setForm({ ...form, verification: e.target.value })}>
          <option value="unverified">Not verified</option><option value="verified">Verified</option><option value="failed">Fix did not hold</option></select></label>
        <div className="small muted">Skills run: {(open.skills?.executed ?? []).map((x: any) => `${x.skill} (${x.outcome.replace("_", " ")})`).join(", ") || "none"}. Running a skill is not the same as it helping.</div>
      </div>
    </Drawer>}
  </>;
}

function Datasets() {
  const { toast } = useFeedback();
  const sets = useLoad(() => api<any[]>("/ml/learning/datasets"), []);
  const [name, setName] = useState("");
  const [purpose, setPurpose] = useState("incident_similarity");
  const [method, setMethod] = useState("grouped");
  const build = async (e: FormEvent) => {
    e.preventDefault();
    try {
      const d = await api("/ml/learning/datasets", { method: "POST", body: { name, purpose, method } });
      toast(`Built ${d.name} v${d.version}: ${d.counts.train} train, ${d.counts.eval} eval.`);
      sets.reload();
    } catch (err: any) {
      toast(err?.message ?? "Could not build the dataset", "bad");
    }
  };
  return <>
    <h2>Datasets</h2>
    <div className="small muted">Built from approved records only, de-duplicated, with every incident, deployment and document family kept on one side of the split. Exports carry features and labels, never evidence text.</div>
    <form className="row wrap" onSubmit={build} aria-label="Build a dataset">
      <label>Name<input value={name} onChange={(e) => setName(e.target.value)} required pattern="[a-z0-9][a-z0-9-]+" /></label>
      <label>Purpose<select value={purpose} onChange={(e) => setPurpose(e.target.value)}>{["incident_similarity", "skill_ranking", "retrieval", "anomaly"].map((p) => <option key={p}>{p}</option>)}</select></label>
      <label>Split<select value={method} onChange={(e) => setMethod(e.target.value)}><option value="grouped">grouped</option><option value="chronological">chronological</option></select></label>
      <button type="submit">Build</button>
    </form>
    {sets.data ? <ul className="small">{sets.data.map((d) => <li key={d.id}><strong>{d.name} v{d.version}</strong> {d.purpose} · {d.counts.train} train / {d.counts.eval} eval · {d.split.duplicates_dropped} duplicates dropped ·
      <span className="mono"> {d.checksum.slice(0, 12)}</span> · <a href={`${API}/ml/learning/datasets/${d.id}/export`}>Export JSONL</a></li>)}</ul> : <Loading load={sets} />}
  </>;
}

/** Technical configuration: modes, training, promotion and rollback, reviews and datasets. */
export function MLAdmin() {
  const { can } = useAuth();
  const status = useLoad(() => api("/ml/status"), []);
  return <div className="stack">
    <PageHead title="Machine learning" sub="Switches, models and data. Every change here is audited; none of it grants infrastructure access." />
    {status.error ? <ErrorBox error={status.error} onRetry={status.reload} /> : status.data ? <div className="grid cards">{Object.entries(status.data.capabilities).map(([n, c]: any) =>
      <CapabilityForm key={n} name={n} c={c} onSaved={status.reload} />)}</div> : <Loading load={status} />}
    {can("ml.admin") && <><Models /><h2>Train</h2><div className="small muted">Fits on the earlier 80% of the history and evaluates on the rest. Needs {">"}= 7 days of usable history by default.</div><Train /></>}
    {can("learning.review") && <Review />}
    {can("learning.export") && <Datasets />}
  </div>;
}
