import { Fragment, type ReactNode } from "react";
import { CopyButton } from "./ui";

/** Markdown to React elements. Safe by construction: nothing is ever parsed as HTML, so a tag in
 *  the text stays text, and links keep only http, https, mailto and same-site paths. Model output
 *  and teammates' notes are both untrusted here. */
export function safeUrl(url: string): string | null {
  const clean = url.trim();
  if (/[\u0000-\u001f]/.test(clean)) return null;
  if (/^(https?:|mailto:)/i.test(clean)) return clean;
  if (/^\/(?![/\\])/.test(clean)) return clean;
  return null;
}

const INLINE = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\*[^*\n]+\*)|(\[[^\]\n]+\]\([^)\s]+\))/g;

export function inline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  for (const m of text.matchAll(INLINE)) {
    const at = m.index;
    if (at > last) out.push(text.slice(last, at));
    const token = m[0];
    if (m[1]) out.push(<code key={at}>{token.slice(1, -1)}</code>);
    else if (m[2]) out.push(<strong key={at}>{inline(token.slice(2, -2))}</strong>);
    else if (m[3]) out.push(<em key={at}>{inline(token.slice(1, -1))}</em>);
    else {
      const [, label, url] = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(token)!;
      const href = safeUrl(url);
      out.push(href ? <a key={at} href={href} target="_blank" rel="noreferrer noopener">{label}</a> : <Fragment key={at}>{label}</Fragment>);
    }
    last = at + token.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

const cells = (row: string) => row.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
const isTableRule = (row?: string) => !!row && /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(row) && row.includes("|");

export function Markdown({ text }: { text: string }) {
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const out: ReactNode[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i], key = i;
    if (!line.trim()) { i++; continue; }
    const fence = /^```(\S*)/.exec(line);
    if (fence) {
      const body: string[] = [];
      for (i++; i < lines.length && !lines[i].startsWith("```"); i++) body.push(lines[i]);
      i++;
      const code = body.join("\n");
      out.push(<div key={key} className="code-block"><CopyButton text={code} /><pre><code>{code}</code></pre></div>);
      continue;
    }
    const heading = /^(#{1,4})\s+(.*)$/.exec(line);
    if (heading) {
      out.push(<h4 key={key} className={`md-h${heading[1].length}`}>{inline(heading[2])}</h4>);
      i++;
      continue;
    }
    if (line.includes("|") && isTableRule(lines[i + 1])) {
      const head = cells(line);
      const rows: string[][] = [];
      for (i += 2; i < lines.length && lines[i].includes("|") && lines[i].trim(); i++) rows.push(cells(lines[i]));
      out.push(<div key={key} className="table-wrap"><table><thead><tr>{head.map((c, n) => <th key={n}>{inline(c)}</th>)}</tr></thead>
        <tbody>{rows.map((r, n) => <tr key={n}>{r.map((c, k) => <td key={k}>{inline(c)}</td>)}</tr>)}</tbody></table></div>);
      continue;
    }
    if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\./.test(line);
      const items: string[] = [];
      for (; i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i]); i++) items.push(lines[i].replace(/^\s*([-*]|\d+\.)\s+/, ""));
      const li = items.map((t, n) => <li key={n}>{inline(t)}</li>);
      out.push(ordered ? <ol key={key}>{li}</ol> : <ul key={key}>{li}</ul>);
      continue;
    }
    const para: string[] = [];
    for (; i < lines.length && lines[i].trim() && !/^(```|#{1,4}\s|\s*([-*]|\d+\.)\s)/.test(lines[i]); i++) para.push(lines[i]);
    if (!para.length) para.push(lines[i++]);
    out.push(<p key={key}>{para.map((p, n) => <Fragment key={n}>{n > 0 && <br />}{inline(p)}</Fragment>)}</p>);
  }
  return <div className="md">{out}</div>;
}
