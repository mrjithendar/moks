import { describe, expect, it } from "vitest";
import { LLM_PRESETS, presetOf } from "./Admin";

describe("LLM presets", () => {
  it("offers the major providers and points compatible servers at their endpoints", () => {
    expect(LLM_PRESETS.anthropic.provider).toBe("anthropic");
    expect(LLM_PRESETS.google.provider).toBe("gemini");
    expect(LLM_PRESETS.openai.provider).toBe("openai");
    expect(LLM_PRESETS.mistral).toMatchObject({ provider: "openai", endpoint: "https://api.mistral.ai/v1" });
  });
  it("recognises a saved provider again, including an unlisted compatible server", () => {
    expect(presetOf({ provider: "anthropic", endpoint: "" })).toBe("anthropic");
    expect(presetOf({ provider: "openai", endpoint: "https://api.groq.com/openai/v1" })).toBe("groq");
    expect(presetOf({ provider: "openai", endpoint: "http://vllm:8000/v1" })).toBe("compatible");
  });
  it("only prices models it lists, and never a key value", () => {
    expect(LLM_PRESETS.anthropic.prices?.["claude-opus-5-5"]).toEqual(["4", "20"]);
    for (const p of Object.values(LLM_PRESETS)) if (p.key) expect(p.key).toMatch(/^MOKS_CRED_[A-Z0-9_]+$/);
  });
});
