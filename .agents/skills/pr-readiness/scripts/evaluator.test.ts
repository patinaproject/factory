import { describe, expect, it } from "bun:test";
import { evaluateReadiness } from "./evaluator.ts";
import { parsePolicy, produceCommand, rulesFromPolicy } from "./policy.ts";
import { freezePacket, sha256, type CandidateIdentity, type Observation, type ObligationRule } from "./domain.ts";

const candidate: CandidateIdentity = { repository: "o/r", pullRequest: 1, base: "main", mergeBase: "m", head: "h", patchId: "p", diffDigest: "d", requirementsDigest: "req", policyDigest: "pol" };
const approval = { kind: "approval", reviewer: "ted", reviewId: 9, url: "https://github.com/o/r/pull/1#pullrequestreview-9" } as const;
const observation = (obligation: Observation["obligation"], extra: Partial<Observation> = {}): Observation => ({
  id: `${obligation}@h`, obligation, candidate, inputDigest: "input", scope: "scope", artifactRefs: [], producerVersion: "1", capturedAt: "2026-10-01T00:00:00Z", verdict: "pass", provenance: approval,
  ...extra,
});
const rule = (obligation: Observation["obligation"], applies = true): ObligationRule => ({ obligation, spec: { producer: "approval" }, applies: () => applies });

describe("pr-readiness evaluator", () => {
  it("accepts current observations and emits a frozen packet", () => {
    const rules = rulesFromPolicy(parsePolicy(undefined));
    const report = evaluateReadiness({ candidate, observations: rules.map((r) => observation(r.obligation)), rules });
    expect(report.ready).toBe(true);
    expect(Object.isFrozen(report.packet)).toBe(true);
    const { digest, ...unsigned } = report.packet!;
    expect(digest).toBe(sha256(unsigned));
    expect(report.packet!.provenanceRefs).toEqual(Array(6).fill(approval.url));
  });
  it("rejects an observation whose provenance is not the declared producer", () => {
    const checks = { kind: "checks", checks: [{ name: "Unit tests", checkRunId: 1, conclusion: "success", url: "u" }] } as const;
    const report = evaluateReadiness({ candidate, observations: [observation("standards", { provenance: checks })], rules: [rule("standards")] });
    expect(report.decisions[0]).toMatchObject({ validity: "invalid", reason: "standards observation was not produced by approval" });
  });
  it("rejects an observation without provenance as malformed", () => {
    const { provenance: _, ...bare } = observation("standards");
    const report = evaluateReadiness({ candidate, observations: [bare as Observation], rules: [rule("standards")] });
    expect(report.errors).toEqual(["malformed observation"]);
    expect(report.ready).toBe(false);
  });
  it("records behavior as not applicable for documentation-only candidates", () => {
    const docs = { ...candidate, behaviorApplicable: false };
    const report = evaluateReadiness({ candidate: docs, observations: [], rules: rulesFromPolicy(parsePolicy(undefined)).filter((r) => r.obligation === "behavior") });
    expect(report.decisions).toEqual([{ obligation: "behavior", validity: "not-applicable", reason: "change does not require behavior evidence" }]);
  });
  it("rejects stale runtime evidence", () => {
    const stale = observation("behavior", { candidate: { ...candidate, head: "old" } });
    expect(evaluateReadiness({ candidate, observations: [stale], rules: [rule("behavior")] }).decisions[0]!.validity).toBe("rerun-required");
  });
  it("keeps packet immutable", () => {
    const packet = freezePacket({ version: 1, candidate, observationIds: [], decisions: [], requirementsDigest: "req", policyDigest: "pol", bodyDigest: "body", provenanceRefs: [], artifactRefs: [] });
    expect(() => { (packet.candidate as { head: string }).head = "x"; }).toThrow();
  });
});

describe("pr-readiness policy", () => {
  it("requires approval for every obligation when the base has no policy file", () => {
    const policy = parsePolicy(undefined);
    expect(policy.obligations).toEqual({ standards: { producer: "approval" }, spec: { producer: "approval" }, hygiene: { producer: "approval" }, tests: { producer: "approval" }, lint: { producer: "approval" }, behavior: { producer: "approval" } });
    expect(policy.digest).toBe(sha256({ evaluator: "pr-readiness/4", raw: null }));
  });
  it("defaults an undeclared obligation to approval", () => {
    const raw = JSON.stringify({ version: 1, obligations: { tests: { producer: "checks", checks: ["Unit tests"] } } });
    const policy = parsePolicy(raw);
    expect(policy.obligations.tests).toEqual({ producer: "checks", checks: ["Unit tests"] });
    expect(policy.obligations.lint).toEqual({ producer: "approval" });
    expect(policy.digest).toBe(sha256({ evaluator: "pr-readiness/4", raw }));
  });
  const rejected: [string, unknown, string][] = [
    ["a wrong version", { version: 2, obligations: {} }, ".github/pr-readiness.json version must be 1"],
    ["an unknown obligation", { version: 1, obligations: { vibes: { producer: "approval" } } }, ".github/pr-readiness.json names unknown obligation: vibes"],
    ["an unknown producer", { version: 1, obligations: { tests: { producer: "artifact" } } }, ".github/pr-readiness.json tests names unknown producer: artifact"],
    ["empty checks", { version: 1, obligations: { tests: { producer: "checks", checks: [] } } }, ".github/pr-readiness.json tests checks must be a non-empty list of check names"],
    ["a non-string check", { version: 1, obligations: { tests: { producer: "checks", checks: [3] } } }, ".github/pr-readiness.json tests checks must be a non-empty list of check names"],
    ["a non-boolean allowSkipped", { version: 1, obligations: { hygiene: { producer: "checks", checks: ["Check diff hygiene"], allowSkipped: "yes" } } }, ".github/pr-readiness.json hygiene allowSkipped must be a boolean"],
  ];
  for (const [what, file, message] of rejected) it(`rejects ${what}`, () => { expect(() => parsePolicy(JSON.stringify(file))).toThrow(message); });

  it("derives produce commands from the spec", () => {
    const ctx = { repository: "o/r", pullRequest: 7, head: "0123456789abcdef", author: "bot" };
    expect(produceCommand({ producer: "approval" }, ctx)).toBe("a maintainer other than @bot reviews 0123456789ab and approves it: gh api repos/o/r/pulls/7/reviews -f commit_id=0123456789abcdef -f event=APPROVE");
    expect(produceCommand({ producer: "checks", checks: ["Lint Actions workflows", "Lint Markdown"] }, ctx)).toBe('push the head so CI runs "Lint Actions workflows", "Lint Markdown"');
    expect(produceCommand({ producer: "checks", checks: ["Unit tests"] }, { ...ctx, failedRunIds: [41, 42] })).toBe('push the head so CI runs "Unit tests", or rerun it: gh run rerun 41 --failed --repo o/r && gh run rerun 42 --failed --repo o/r');
  });
});
