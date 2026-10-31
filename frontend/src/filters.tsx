import { type ReactNode, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Drawer, Icon, useIsMobile } from "./ui";

type Value = string | number | boolean | string[];

/** Filter state in the URL, so a view can be shared and survives opening a detail and coming back.
 *  Only non-sensitive values belong here: never log search text, IDs, or anything typed into a query. */
export function useUrlFilters<T extends Record<string, Value>>(defaults: T): [T, (patch: Partial<T>) => void, () => void] {
  const [params, setParams] = useSearchParams();
  const first = useRef(defaults).current;
  const raw = params.toString();
  const values = useMemo(() => {
    const p = new URLSearchParams(raw);
    const out: Record<string, Value> = { ...first };
    for (const [k, d] of Object.entries(first)) {
      const v = p.get(k);
      if (v === null) continue;
      if (Array.isArray(d)) out[k] = v.split(",").filter(Boolean);
      else if (typeof d === "number") out[k] = Number.isFinite(Number(v)) ? Number(v) : d;
      else if (typeof d === "boolean") out[k] = v === "1";
      else out[k] = v;
    }
    return out as T;
  }, [raw, first]);
  const set = useCallback((patch: Partial<T>) => {
    setParams((prev) => {
      const next = new URLSearchParams(prev);
      for (const [k, v] of Object.entries(patch) as [string, Value][]) {
        const text = Array.isArray(v) ? v.join(",") : typeof v === "boolean" ? (v ? "1" : "") : String(v);
        const same = JSON.stringify(v) === JSON.stringify(first[k]);
        if (same || text === "") next.delete(k); else next.set(k, text);
      }
      return next;
    }, { replace: true });
  }, [first, setParams]);
  const clear = useCallback(() => set(first), [first, set]);
  return [values, set, clear];
}

export interface Chip { key: string; label: string; onRemove?: () => void }

/** Chips stay visible when the panel is closed: a collapsed filter is still filtering. On phones the
 *  controls open in a sheet, and `onApply` lets a page run an expensive query once, on Apply. */
export function FilterPanel({ chips, onClear, children, advanced, search, actions, scope, onApply, applying, applyLabel = "Apply", label = "Filters" }: {
  chips: Chip[]; onClear?: () => void; children?: ReactNode; advanced?: ReactNode; search?: ReactNode; actions?: ReactNode; scope?: ReactNode;
  onApply?: () => void; applying?: boolean; applyLabel?: string; label?: string;
}) {
  const mobile = useIsMobile();
  const [open, setOpen] = useState(false);
  const body = useId();
  const count = chips.length;
  const button = (mobile || advanced) && (
    <button type="button" className={`ghost filter-btn${count ? " has-active" : ""}`} aria-expanded={open} aria-controls={mobile ? undefined : body} onClick={() => setOpen(!open)}>
      <Icon name="filter" size={16} /><span>{label}</span>{count > 0 && <span className="filter-count" aria-label={`${count} active`}>{count}</span>}
    </button>
  );
  return (
    <section className="fp" aria-label={label}>
      <div className="fp-bar">
        {search}{!mobile && children}{button}{actions}
      </div>
      {!mobile && advanced && <div id={body} className="fp-more" hidden={!open}>{advanced}</div>}
      {!mobile && onApply && <div className="fp-apply">{scope && <span className="small muted grow">{scope}</span>}<button type="button" onClick={onApply} disabled={applying}>{applying ? "Searching…" : applyLabel}</button></div>}
      {count > 0 && (
        <div className="fp-chips" aria-label="Active filters">
          {chips.map((c) => <span key={c.key} className="chip"><span className="trunc">{c.label}</span>
            {c.onRemove && <button type="button" className="chip-x" aria-label={`Remove filter ${c.label}`} onClick={c.onRemove}><Icon name="close" size={12} /></button>}</span>)}
          {onClear && <button type="button" className="ghost small" onClick={onClear}>Clear all</button>}
        </div>
      )}
      {mobile && open && (
        <Drawer title={label} onClose={() => setOpen(false)} footer={<>
          {scope && <div className="small muted">{scope}</div>}
          <div className="row between">
            {onClear ? <button type="button" className="ghost" onClick={onClear}>Reset</button> : <span />}
            <button type="button" disabled={applying} onClick={() => { onApply?.(); setOpen(false); }}>{onApply ? applyLabel : "Done"}</button>
          </div></>}>
          <div className="fp-sheet">{children}{advanced}</div>
        </Drawer>
      )}
    </section>
  );
}

/** Searchable multi-select dropdown. Nothing selected means all the options. */
export function MultiSelect({ label, options, value, onChange, allLabel }: { label: string; options: { value: string; label?: string }[]; value: string[]; onChange: (v: string[]) => void; allLabel: string }) {
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => { if (!box.current?.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") { e.stopPropagation(); setOpen(false); } };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc, true);
    return () => { document.removeEventListener("mousedown", away); document.removeEventListener("keydown", esc, true); };
  }, [open]);
  const shown = options.filter((o) => (o.label ?? o.value).toLowerCase().includes(q.toLowerCase()));
  const name = (v: string) => options.find((o) => o.value === v)?.label ?? v;
  const text = !value.length ? allLabel : value.length === 1 ? name(value[0]) : `${value.length} selected`;
  return (
    <div className="ms" ref={box}>
      <span className="ms-label">{label}</span>
      <button type="button" className="ms-button" aria-haspopup="listbox" aria-expanded={open} aria-label={`${label}: ${text}`} onClick={() => setOpen(!open)}>
        <span>{text}</span><span aria-hidden>▾</span>
      </button>
      {open && <div className="ms-menu" role="listbox" aria-multiselectable aria-label={label}>
        {options.length > 6 && <input autoFocus aria-label={`Search ${label}`} placeholder="Search…" value={q} onChange={(e) => setQ(e.target.value)} />}
        <button type="button" role="option" aria-selected={!value.length} className={`ms-option ${!value.length ? "on" : ""}`} onClick={() => { onChange([]); setOpen(false); }}>{allLabel}</button>
        {shown.slice(0, 200).map((o) => {
          const on = value.includes(o.value);
          return <button type="button" role="option" aria-selected={on} key={o.value} className={`ms-option ${on ? "on" : ""}`}
            onClick={() => onChange(on ? value.filter((v) => v !== o.value) : [...value, o.value])}><span className="ms-check" aria-hidden>{on ? "✓" : ""}</span><span className="trunc">{o.label ?? o.value}</span></button>;
        })}
        {!shown.length && <div className="small muted">No matches.</div>}
      </div>}
    </div>
  );
}
