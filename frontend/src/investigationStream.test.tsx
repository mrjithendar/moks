import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { parseEvent, useInvestigationEvents } from "./investigationStream";

class FakeES {
  static all: FakeES[] = [];
  static CLOSED = 2;
  readyState = 1;
  closed = false;
  onopen?: () => void;
  onerror?: () => void;
  handlers: Record<string, ((m: { data: string }) => void)[]> = {};
  constructor(public url: string) { FakeES.all.push(this); }
  addEventListener(t: string, f: (m: { data: string }) => void) { (this.handlers[t] ??= []).push(f); }
  close() { this.closed = true; }
  emit(t: string, data: unknown) { for (const f of this.handlers[t] ?? []) f({ data: typeof data === "string" ? data : JSON.stringify(data) }); }
}

const ev = (seq: number, type = "note") => ({ seq, type, at: "2026-10-30T00:00:00Z" });

beforeEach(() => {
  FakeES.all = [];
  vi.stubGlobal("EventSource", FakeES);
});

describe("investigation stream", () => {
  it("clears the timeline when the route changes and ignores the old subscription", () => {
    const { result, rerender } = renderHook(({ id }) => useInvestigationEvents(id, () => {}), { initialProps: { id: "a" } });
    act(() => FakeES.all[0].emit("note", ev(1)));
    expect(result.current.events).toHaveLength(1);
    rerender({ id: "b" });
    expect(result.current.events).toHaveLength(0);
    expect(FakeES.all[0].closed).toBe(true);
    act(() => FakeES.all[0].emit("note", ev(2))); // late callback from a
    expect(result.current.events).toHaveLength(0);
    act(() => FakeES.all[1].emit("note", ev(1)));
    expect(result.current.events.map((e) => e.seq)).toEqual([1]);
  });

  it("deduplicates replays and survives malformed payloads", () => {
    const { result } = renderHook(() => useInvestigationEvents("a", () => {}));
    act(() => {
      FakeES.all[0].emit("note", ev(2));
      FakeES.all[0].emit("note", ev(2));
      FakeES.all[0].emit("note", "{not json");
      FakeES.all[0].emit("note", { type: "note" });
      FakeES.all[0].emit("note", ev(1));
    });
    expect(result.current.events.map((e) => e.seq)).toEqual([1, 2]);
  });

  it("reports the real connection state", () => {
    const { result } = renderHook(() => useInvestigationEvents("a", () => {}));
    expect(result.current.stream).toBe("connecting");
    act(() => FakeES.all[0].onopen?.());
    expect(result.current.stream).toBe("live");
    act(() => { FakeES.all[0].readyState = 0; FakeES.all[0].onerror?.(); });
    expect(result.current.stream).toBe("reconnecting");
    act(() => { FakeES.all[0].readyState = 2; FakeES.all[0].onerror?.(); });
    expect(result.current.stream).toBe("failed");
    act(() => FakeES.all[0].emit("end", {}));
    expect(result.current.stream).toBe("ended");
  });

  it("refreshes details only for events that change them", () => {
    const refresh = vi.fn();
    renderHook(() => useInvestigationEvents("a", refresh));
    act(() => { FakeES.all[0].emit("note", ev(1)); FakeES.all[0].emit("report", ev(2, "report")); });
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("rejects payloads without a sequence", () => {
    expect(parseEvent("null")).toBeNull();
    expect(parseEvent('{"seq":"1","type":"x"}')).toBeNull();
  });
});
