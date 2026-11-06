import type { ReactNode } from "react";
import { ActionMenu, Identifier, type MenuItem, useIsMobile } from "./ui";

/** The list a resource (a pod, later a workload, an incident…) is shown in. Its name is one line, cut at the
 *  end, with the whole value and Copy one tap or focus away; it never wraps a character at a time.
 *  Below 768px: status, name and the main action first, and the other fields in an expandable row. */
export interface Field<T> { key: string; head: string; cell: (row: T) => ReactNode }
export type Tone = "ok" | "warn" | "bad" | "info" | "neutral";

const MARK: Record<Tone, string> = { ok: "●", warn: "▲", bad: "■", info: "◆", neutral: "○" };

export function StatusPill({ tone, text }: { tone: Tone; text: string }) {
  return <span className={`badge ${tone} status-pill`}><span aria-hidden="true">{MARK[tone]} </span>{text}</span>;
}

export function ResourceTable<T>({ label, rows, rowKey, name, what, status, action, fields, menu }: {
  label: string; rows: T[]; rowKey: (r: T) => string; name: (r: T) => string; what: string;
  status: (r: T) => { tone: Tone; text: string }; action: { label: string; run: (r: T) => void };
  fields: Field<T>[]; menu?: (r: T) => MenuItem[];
}) {
  const phone = useIsMobile(767);
  if (phone) {
    return (
      <ul className="rt-list" aria-label={label}>
        {rows.map((r) => {
          const s = status(r);
          return (
            <li key={rowKey(r)} className="rt-card" data-testid="rt-row">
              <div className="rt-top"><StatusPill {...s} /><Identifier name={name(r)} what={what} /></div>
              <div className="rt-actions">
                <button type="button" className="ghost" onClick={() => action.run(r)} aria-label={`${action.label}: ${name(r)}`}>{action.label}</button>
                {menu && <ActionMenu label={`Actions for ${name(r)}`} items={menu(r)} text="Actions" />}
              </div>
              <details className="rt-more">
                <summary>More about this {what.split(" ")[0]}</summary>
                <dl>{fields.map((f) => <div key={f.key}><dt>{f.head}</dt><dd>{f.cell(r)}</dd></div>)}</dl>
              </details>
            </li>
          );
        })}
      </ul>
    );
  }
  return (
    <div className="table-wrap rt-wrap" role="region" aria-label={label} tabIndex={0}>
      <table className="rt">
        <thead><tr><th scope="col">Status</th><th scope="col">Name</th>{fields.map((f) => <th key={f.key} scope="col">{f.head}</th>)}<th scope="col"><span className="sr-only">Actions</span></th></tr></thead>
        <tbody>{rows.map((r) => (
          <tr key={rowKey(r)} data-testid="rt-row">
            <td><StatusPill {...status(r)} /></td>
            <td className="td-name"><Identifier name={name(r)} what={what} /></td>
            {fields.map((f) => <td key={f.key}>{f.cell(r)}</td>)}
            <td className="td-menu rt-act">
              <button type="button" className="ghost small" onClick={() => action.run(r)} aria-label={`${action.label}: ${name(r)}`}>{action.label}</button>
              {menu && <ActionMenu label={`Actions for ${name(r)}`} items={menu(r)} />}
            </td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}
