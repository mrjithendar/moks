// The UI, the CSV and a proposal for kindnet-cni, all read from the backend engine's real output
// (src/fixtures/rightsizing.json, generated from finops.rightsize(); see its _source). Regenerate it from backend/ with
//   python -c "import tests.test_rightsizing_changes as t; t.write_frontend_fixture()"
import { describe, expect, it } from "vitest";
import data from "./fixtures/rightsizing.json";
import { capacityText, CSV_COLUMNS, diffLines, type Pricing, proposeBlocker, recsOf, resizePlan, type Row, savingText } from "./rightsizing";
import { toCsv } from "./pages/Insights";

const rows = data.rows as unknown as Row[];
const pricing = data.pricing as Pricing;
const kindnet = rows.find((r) => r.container === "kindnet-cni")!;
const checkout = rows.find((r) => r.workload === "Deployment/checkout")!;
const [mem, cpu] = ["memory", "cpu"].map((k) => recsOf(kindnet).find((x) => x.resource === k)!);

describe("kindnet-cni", () => {
  it("is a memory increase and a separate CPU reduction, with true current values", () => {
    expect(kindnet.current).toEqual({ cpu_request: "100m", cpu_limit: "100m", mem_request: "50Mi", mem_limit: "50Mi" });
    expect(mem.action).toBe("increase");
    expect(mem.changes.map((c) => [c.field, c.current, c.recommended])).toEqual([["mem_request", "50Mi", "64Mi"], ["mem_limit", "50Mi", "96Mi"]]);
    expect(cpu.action).toBe("reduce");
    expect(cpu.changes.map((c) => [c.field, c.current, c.recommended])).toEqual([["cpu_request", "100m", "10m"]]);
  });

  it("CSV rows carry the same four values as the engine, one row per recommendation", () => {
    const csv = toCsv(recsOf(kindnet).map((rec) => ({ r: kindnet, rec, pricing })), CSV_COLUMNS);
    const [head, memRow, cpuRow] = csv.trim().split("\r\n").map((l) => l.split(/","/).map((c) => c.replace(/^"|"$/g, "")));
    const col = (row: string[], name: string) => row[head.indexOf(name)];
    expect(col(memRow, "Resource")).toBe("Memory");
    expect([col(memRow, "Memory request now"), col(memRow, "Memory request recommended"), col(memRow, "Memory limit now"), col(memRow, "Memory limit recommended")]).toEqual(["50Mi", "64Mi", "50Mi", "96Mi"]);
    expect([col(memRow, "CPU request now"), col(memRow, "CPU request recommended")]).toEqual(["100m", "100m"]); // the memory row changes no CPU
    expect([col(cpuRow, "CPU request now"), col(cpuRow, "CPU request recommended"), col(cpuRow, "CPU limit recommended")]).toEqual(["100m", "10m", "100m"]);
    expect(col(cpuRow, "Memory request recommended")).toBe("50Mi");
    expect(col(cpuRow, "Estimated saving per month")).toBe(""); // fixed price: capacity, not money
    expect(col(cpuRow, "Capacity")).toBe("frees 450m CPU");
  });

  it("a proposal patches exactly the changed fields", () => {
    expect(resizePlan(kindnet, mem, 7).params).toEqual({ container: "kindnet-cni", requests: { memory: "64Mi" }, limits: { memory: "96Mi" } });
    expect(resizePlan(kindnet, cpu, 7).params).toEqual({ container: "kindnet-cni", requests: { cpu: "10m" }, limits: {} });
    expect(diffLines(kindnet, mem)).toEqual([
      { path: "containers[kindnet-cni].resources.requests.memory", from: "50Mi", to: "64Mi" },
      { path: "containers[kindnet-cni].resources.limits.memory", from: "50Mi", to: "96Mi" }]);
  });

  it("cannot be proposed, and says why", () => {
    expect(proposeBlocker(kindnet, () => true)).toMatch(/Deployments only.*DaemonSet kindnet/);
    expect(proposeBlocker(checkout, () => false)).toMatch(/action\.request/);
    expect(proposeBlocker(checkout, () => true)).toBeNull();
  });

  it("shows capacity under a fixed price and money only as an estimate under unit prices", () => {
    expect(capacityText(mem)).toBe("reserves 70 MiB memory");
    expect(savingText(cpu, pricing)).toBeNull();
    expect(savingText(cpu, { kind: "per_unit", description: "x" })).toMatch(/^saves \d+\.\d\d per month$/);
  });
});
