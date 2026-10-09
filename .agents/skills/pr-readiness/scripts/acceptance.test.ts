import { describe, expect, it } from "bun:test";
import { mkdtempSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { evaluateReadiness } from "./evaluator.ts";
import { FilePacketStore } from "./adapters.ts";
import { sha256, type CandidateIdentity, type Observation } from "./domain.ts";

const candidate: CandidateIdentity = { repository: "o/r", pullRequest: 7, base: "main", baseSha: "b", baseRef: "main", mergeBase: "m", head: "h", patchId: "p", diffDigest: "d", requirementsDigest: "rq", policyDigest: "po", reviewContextDigest: "rv" };
const observation = (obligation: Observation["obligation"], patch = candidate, extra: Partial<Observation> = {}): Observation => ({ id: `${obligation}-${patch.head}`, obligation, candidate: patch, inputDigest: "in", scope: "runtime", artifactRefs: ["artifact"], producerVersion: "v", capturedAt: "2026-01-01T00:00:00Z", verdict: "pass", ...(obligation === "behavior" ? { executableArtifact: "bin", target: "linux", runtime: "node", environment: "ci", fixtures: "fixtures", freshness: "now" } : {}), receipt: { producer: "producer", observationId: `${obligation}-${patch.head}`, candidateHead: patch.head, digest: "receipt", issuedAt: "2026-01-01T00:00:00Z" }, ...extra });
const rules = [{ obligation: "behavior" as const, producer: "producer", requireReceipt: true, applies: () => true }];

describe("readiness acceptance matrix", () => {
  it("fails closed for malformed, foreign, duplicate, and expired evidence", () => {
    expect(evaluateReadiness({ candidate, observations: [{ nope: true } as never], rules }).ready).toBe(false);
    expect(evaluateReadiness({ candidate, observations: [observation("behavior", { ...candidate, pullRequest: 8 })], rules }).decisions[0]?.validity).toBe("invalid");
    const duplicate = observation("behavior");
    expect(evaluateReadiness({ candidate, observations: [duplicate, { ...duplicate }], rules }).decisions[0]?.validity).toBe("invalid");
    expect(evaluateReadiness({ candidate, observations: [observation("behavior", candidate, { expiresAt: "2025-01-01T00:00:00Z" })], rules, now: "2026-01-01T00:00:00Z" }).decisions[0]?.validity).toBe("rerun-required");
  });
  it("requires all runtime equivalence dimensions, including executable artifact", () => {
    const old = observation("behavior", { ...candidate, head: "old" }, { executableArtifact: "bin-a", target: "linux", runtime: "node", environment: "ci", fixtures: "f", scope: "all", freshness: "2026-01-01", artifactDigest: sha256("a") });
    const report = evaluateReadiness({ candidate, observations: [old], rules, expectedExecution: { behavior: { executableArtifact: "bin-b", target: "linux", runtime: "node", environment: "ci", fixtures: "f", scope: "all", freshness: "2026-01-01", artifactDigest: sha256("b") } } });
    expect(report.decisions[0]?.validity).toBe("rerun-required");
  });
  it("invalidates test evidence when the base identity changes", () => {
    const old = observation("tests", { ...candidate, baseSha: "old-base", mergeBase: "old-merge" });
    const report = evaluateReadiness({ candidate, observations: [old], rules: [{ obligation: "tests", producer: "producer", requireReceipt: true, applies: () => true }] });
    expect(report.decisions[0]?.validity).toBe("rerun-required");
  });
  it("stores immutable packets and rejects replacement", () => {
    const root = mkdtempSync(join(tmpdir(), "readiness-store-")); const store = new FilePacketStore(root);
    const report = evaluateReadiness({ candidate, observations: [observation("behavior")], rules }); expect(report.packet).toBeDefined();
    const first = store.putIfAbsent("key", report.packet!); expect(store.get("key")?.digest).toBe(first.digest);
    expect(() => store.putIfAbsent("key", { ...report.packet!, bodyDigest: "tampered", digest: "bad" })).toThrow();
    expect(readFileSync(join(root, `${sha256("key")}.json`), "utf8")).toContain(first.digest);
  });
});
