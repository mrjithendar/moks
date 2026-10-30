import { useCallback, useEffect, useRef, useState, type RefObject } from "react";

const NEAR_BOTTOM = 120;
const OWN_ACTION_MS = 500; // opening a section is the reader's own action, not new activity

/** Follows new content only while the reader is at the bottom. Reading earlier messages is never
 *  interrupted: `notify` raises "New activity" instead. */
export function useFollow(scroller: RefObject<HTMLElement | null>, content: RefObject<HTMLElement | null>) {
  const pinned = useRef(true);
  const toggled = useRef(0);
  const [fresh, setFresh] = useState(false);

  const jump = useCallback(() => {
    const el = scroller.current;
    if (el) el.scrollTop = el.scrollHeight;
    pinned.current = true;
    setFresh(false);
  }, [scroller]);

  useEffect(() => {
    const el = scroller.current, body = content.current;
    if (!el) return;
    const onScroll = () => {
      pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM;
      if (pinned.current) setFresh(false);
    };
    const onToggle = () => { toggled.current = Date.now(); };
    el.addEventListener("scroll", onScroll, { passive: true });
    el.addEventListener("toggle", onToggle, true);
    const grow = typeof ResizeObserver === "function" ? new ResizeObserver(() => {
      if (pinned.current && Date.now() - toggled.current > OWN_ACTION_MS) el.scrollTop = el.scrollHeight;
    }) : null;
    if (body) grow?.observe(body);
    return () => { el.removeEventListener("scroll", onScroll); el.removeEventListener("toggle", onToggle, true); grow?.disconnect(); };
  }, [scroller, content]);

  const notify = useCallback(() => {
    if (pinned.current) { const el = scroller.current; if (el) el.scrollTop = el.scrollHeight; }
    else setFresh(true);
  }, [scroller]);

  return { fresh, jump, notify };
}
