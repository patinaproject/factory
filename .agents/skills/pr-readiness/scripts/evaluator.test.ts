import { describe, expect, it } from "bun:test";
import { evaluateReadiness } from "./evaluator.ts";
import { freezePacket, sha256, type CandidateIdentity, type Observation } from "./domain.ts";

const candidate: CandidateIdentity = { repository: "o/r", pullRequest: 1, base: "main", mergeBase: "m", head: "h", patchId: "p", diffDigest: "d", requirementsDigest: "req", policyDigest: "pol" };
const observation = (obligation: Observation["obligation"], id = obligation): Observation => ({ id, obligation, candidate, inputDigest: "input", scope: "scope", artifactRefs: [], producerVersion: "1", capturedAt: new Date().toISOString(), verdict: "pass", ...(obligation === "behavior" ? { executableArtifact: "bin", target: "linux", runtime: "node", environment: "ci", fixtures: "fixtures", freshness: "now" } : {}) });
const rules = ["standards", "spec", "hygiene", "tests", "lint", "behavior"].map((obligation) => ({ obligation: obligation as Observation["obligation"], producer: "test", applies: () => true }));

describe("pr-readiness evaluator", () => {
  it("accepts current observations and emits a frozen packet", () => {
    const report = evaluateReadiness({ candidate, observations: rules.map((r) => observation(r.obligation)), rules });
    expect(report.ready).toBe(true);
    expect(Object.isFrozen(report.packet)).toBe(true);
    const { digest, ...unsigned } = report.packet!;
    expect(digest).toBe(sha256(unsigned));
  });
  it("records non-applicable obligations", () => {
    const report = evaluateReadiness({ candidate, observations: [], rules: [{ obligation: "behavior", producer: "runtime", applies: () => false }] });
    expect(report.decisions[0]).toMatchObject({ validity: "not-applicable" });
    expect(report.ready).toBe(true);
  });
  it("records behavior as not applicable for documentation-only candidates", () => {
    const report = evaluateReadiness({ candidate: { ...candidate, behaviorApplicable: false }, observations: [], rules: [{ obligation: "behavior", producer: "runtime", applies: (value) => value.behaviorApplicable !== false }] });
    expect(report.decisions.find((decision) => decision.obligation === "behavior")).toMatchObject({ validity: "not-applicable" });
  });
  it("rejects stale runtime evidence", () => {
    const stale = { ...observation("behavior"), candidate: { ...candidate, head: "old" } };
    const report = evaluateReadiness({ candidate, observations: [stale], rules: [{ obligation: "behavior", producer: "runtime", applies: () => true }] });
    expect(report.decisions[0].validity).toBe("rerun-required");
  });
  it("accepts standards reuse only with unchanged review context", () => {
    const historical = { ...observation("standards"), candidate: { ...candidate, head: "old" } };
    const report = evaluateReadiness({ candidate, observations: [historical], rules: [{ obligation: "standards", producer: "review", applies: () => true }] });
    expect(report.decisions[0].validity).toBe("valid-by-equivalence");
  });
  it("keeps packet immutable", () => {
    const packet = freezePacket({ version: 1, candidate, observationIds: [], decisions: [], requirementsDigest: "req", policyDigest: "pol", bodyDigest: "body", receiptRefs: [], artifactRefs: [] });
    expect(() => { (packet.candidate as { head: string }).head = "x"; }).toThrow();
  });
});
