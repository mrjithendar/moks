import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode, type RefObject } from "react";

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])';

/** Modal behaviour for a drawer or dialog: focus moves in, Tab stays in, Escape closes, the page
 *  behind stops scrolling, and focus returns to whatever opened it. */
export function useModal(ref: RefObject<HTMLElement | null>, open: boolean, onClose: () => void) {
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    if (!open) return;
    const opener = document.activeElement as HTMLElement | null;
    const box = ref.current;
    (box?.querySelector<HTMLElement>("[data-autofocus]") ?? box?.querySelector<HTMLElement>(FOCUSABLE) ?? box)?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.stopPropagation(); close.current(); return; }
      if (e.key !== "Tab" || !box) return;
      const items = [...box.querySelectorAll<HTMLElement>(FOCUSABLE)];
      if (!items.length) { e.preventDefault(); return; }
      const first = items[0], last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", onKey);
    document.body.classList.add("scroll-locked");
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.classList.remove("scroll-locked");
      opener?.focus?.();
    };
  }, [open, ref]);
}

export function Dialog({ title, onClose, children, wide }: { title: string; onClose: () => void; children: ReactNode; wide?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  useModal(ref, true, onClose);
  return (
    <div className="backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div ref={ref} className={`dialog${wide ? " wide" : ""}`} role="dialog" aria-modal="true" aria-label={title} tabIndex={-1}>
        <div className="row between"><h2>{title}</h2><button type="button" className="ghost small" onClick={onClose}>Close</button></div>
        {children}
      </div>
    </div>
  );
}

interface Ask { title: string; body: ReactNode; action: string; danger?: boolean }
interface Feedback { confirm: (a: Ask) => Promise<boolean>; toast: (text: string, kind?: "ok" | "bad") => void }
const Ctx = createContext<Feedback | null>(null);

/** The replacement for window.confirm and window.alert: same decision, same words, keyboard-accessible. */
export function FeedbackProvider({ children }: { children: ReactNode }) {
  const [ask, setAsk] = useState<(Ask & { done: (v: boolean) => void }) | null>(null);
  const [toasts, setToasts] = useState<{ id: number; text: string; kind: string }[]>([]);
  const next = useRef(0);
  const confirm = useCallback((a: Ask) => new Promise<boolean>((done) => setAsk({ ...a, done })), []);
  const toast = useCallback((text: string, kind: "ok" | "bad" = "ok") => {
    const id = next.current++;
    setToasts((t) => [...t, { id, text, kind }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 6000);
  }, []);
  const finish = (v: boolean) => { ask?.done(v); setAsk(null); };
  return (
    <Ctx.Provider value={{ confirm, toast }}>
      {children}
      {ask && (
        <Dialog title={ask.title} onClose={() => finish(false)}>
          <div className="stack">
            <div>{ask.body}</div>
            <div className="row end">
              <button type="button" className="ghost" onClick={() => finish(false)} data-autofocus>Cancel</button>
              <button type="button" className={ask.danger ? "danger" : ""} onClick={() => finish(true)}>{ask.action}</button>
            </div>
          </div>
        </Dialog>
      )}
      <div className="toasts" role="status" aria-live="polite">{toasts.map((t) => <div key={t.id} className={`alert ${t.kind === "bad" ? "error" : "info"}`}>{t.text}</div>)}</div>
    </Ctx.Provider>
  );
}

export function useFeedback(): Feedback {
  const v = useContext(Ctx);
  if (!v) throw new Error("useFeedback outside FeedbackProvider");
  return v;
}

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setDone(true);
      setTimeout(() => setDone(false), 1500);
    } catch { /* the browser refused: the text is still selectable */ }
  }
  return <button type="button" className="ghost small copy" onClick={copy}>{done ? "Copied" : label}</button>;
}

export function useIsMobile(width = 900): boolean {
  const query = `(max-width: ${width}px)`;
  const [mobile, setMobile] = useState(() => typeof window.matchMedia === "function" && window.matchMedia(query).matches);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const m = window.matchMedia(query);
    const on = () => setMobile(m.matches);
    on();
    m.addEventListener("change", on);
    return () => m.removeEventListener("change", on);
  }, [query]);
  return mobile;
}

const ICONS: Record<string, string> = {
  grid: "M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z", chart: "M4 20V4M4 20h16M8 16l4-5 3 3 5-7", lines: "M4 6h16M4 12h16M4 18h10",
  radar: "M12 12l6-6M12 3a9 9 0 1 0 9 9M12 8a4 4 0 1 0 4 4", plus: "M12 5v14M5 12h14", chat: "M4 5h16v11H9l-5 4z", alert: "M12 4l9 16H3zM12 10v4M12 17v.5",
  check: "M4 12l5 5L20 6", tool: "M15 6a4 4 0 0 0-5 5l-6 6 3 3 6-6a4 4 0 0 0 5-5l-3 3-3-3z", cube: "M12 3l8 4.5v9L12 21l-8-4.5v-9zM4 7.5l8 4.5 8-4.5M12 12v9",
  archive: "M4 5h16v4H4zM5 9v10h14V9M10 13h4", book: "M5 4h12a2 2 0 0 1 2 2v14H7a2 2 0 0 1-2-2zM5 18a2 2 0 0 1 2-2h12", spark: "M12 3v6M12 15v6M3 12h6M15 12h6",
  coin: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 7v10M9 15h4a2 2 0 0 0 0-4h-2a2 2 0 0 1 0-4h4", shield: "M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z",
  layers: "M12 3l9 5-9 5-9-5zM3 13l9 5 9-5", plug: "M9 3v5M15 3v5M6 8h12v3a6 6 0 0 1-12 0zM12 17v4", cloud: "M7 18a4 4 0 0 1 0-8 5 5 0 0 1 9.6-1A4.5 4.5 0 0 1 17 18z",
  cpu: "M7 7h10v10H7zM10 3v4M14 3v4M10 17v4M14 17v4M3 10h4M3 14h4M17 10h4M17 14h4", users: "M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM2 21a7 7 0 0 1 14 0M16 3.5a4 4 0 0 1 0 7.5M18 15a7 7 0 0 1 4 6",
  flask: "M10 3h4M11 3v6l-6 10a1.5 1.5 0 0 0 1.3 2h11.4a1.5 1.5 0 0 0 1.3-2l-6-10V3", menu: "M4 6h16M4 12h16M4 18h16", close: "M6 6l12 12M18 6L6 18", collapse: "M15 6l-6 6 6 6", expand: "M9 6l6 6-6 6",
};

export function Icon({ name, size = 18 }: { name: string; size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={ICONS[name]} /></svg>;
}
