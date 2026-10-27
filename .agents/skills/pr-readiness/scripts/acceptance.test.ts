import { describe, expect, it } from "bun:test";
import { mkdtempSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { FilePacketStore, MemoryPacketStore } from "./adapters.ts";
import { sha256, type ValidityDecision } from "./domain.ts";
import type { EvaluationReport } from "./evaluator.ts";
import { AUTHOR, CHECK_NAMES, HEAD, checkRun, fakeForge, invoke, review, type FakeForgeOptions } from "./fake-forge.ts";

function check(options: FakeForgeOptions): { code: number; stderr: string; decision: (obligation: string) => ValidityDecision } {
  const result = invoke(["check", "--repo", "o/r", "--pr", "7"], fakeForge(options));
  const parsed = JSON.parse(result.stdout) as EvaluationReport;
  return { code: result.code, stderr: result.stderr, decision: (obligation) => parsed.decisions.find((d) => d.obligation === obligation)! };
}
const APPROVE = `a maintainer other than @${AUTHOR} reviews 0123456789ab and approves it: gh api repos/o/r/pulls/7/reviews -f commit_id=${HEAD} -f event=APPROVE`;

describe("acceptance: forge-attributed evidence", () => {
  it("publishes a candidate whose checks and maintainer approval are on the head", () => {
    const forge = fakeForge(); const store = new MemoryPacketStore();
    const result = invoke(["publish", "--repo", "o/r", "--pr", "7", "--takeover", "true"], forge, store);
    expect(result.stderr).toBe("");
    expect(result.code).toBe(0);
    expect(forge.markReadyCalls).toEqual([HEAD]);
    expect(store.packets.size).toBe(1);
    expect([...store.packets.values()][0]!.observationIds).toEqual(["standards", "spec", "hygiene", "tests", "lint", "behavior"].map((o) => `${o}@${HEAD}`));
  });

  it("names the producer of a missing check", () => {
    const result = check({ checkRuns: CHECK_NAMES.filter((name) => name !== "Unit tests").map((name) => checkRun(name)) });
    expect(result.code).toBe(1);
    expect(result.decision("tests")).toMatchObject({ validity: "rerun-required", reason: 'missing tests observation: no GitHub Actions run of "Unit tests"', produce: 'push the head so CI runs "Unit tests"' });
    expect(result.stderr).toBe([
      'tests: missing tests observation: no GitHub Actions run of "Unit tests" -> push the head so CI runs "Unit tests"',
      "",
    ].join("\n"));
  });

  it("names the approval command when no maintainer approved", () => {
    const result = check({ reviews: [] });
    expect(result.code).toBe(1);
    expect(result.decision("standards")).toMatchObject({ validity: "rerun-required", reason: "missing standards observation: no maintainer approval at 0123456789ab", produce: APPROVE });
    expect(result.decision("spec")).toMatchObject({ validity: "rerun-required", produce: APPROVE });
  });

  it("reports a running check as pending", () => {
    const result = check({ checkRuns: [...CHECK_NAMES.filter((name) => name !== "Lint Markdown").map((name) => checkRun(name)), checkRun("Lint Markdown", { status: "in_progress", conclusion: null, completedAt: null })] });
    expect(result.decision("lint")).toMatchObject({ validity: "rerun-required", reason: 'missing lint observation: "Lint Markdown" still running' });
  });
});

describe("acceptance: self-approval is closed", () => {
  const ignored: [string, Parameters<typeof review>[0], string][] = [
    ["the PR author", { login: AUTHOR, userType: "Bot" }, `review by PR author @${AUTHOR} ignored`],
    ["a bot", { login: "renovate[bot]", userType: "Bot" }, "review by Bot @renovate[bot] ignored"],
    ["a reader", { permission: "read" }, "review by @ted without write access (read) ignored"],
    ["a user with no access", { permission: "none" }, "review by @ted without write access (none) ignored"],
    ["an older commit", { commitId: "c".repeat(40) }, "review by @ted on older commit cccccccccccc ignored"],
  ];
  for (const [who, fields, note] of ignored) {
    it(`ignores an approval by ${who}`, () => {
      const result = check({ reviews: [review(fields)] });
      expect(result.code).toBe(1);
      expect(result.decision("standards")).toMatchObject({ validity: "rerun-required", reason: `missing standards observation: no maintainer approval at 0123456789ab; ${note}` });
    });
  }

  it("ignores a same-named check run from another app", () => {
    const result = check({ checkRuns: [...CHECK_NAMES.filter((name) => name !== "Unit tests").map((name) => checkRun(name)), checkRun("Unit tests", { appSlug: "patinaproject-goodyear" })] });
    expect(result.decision("tests")).toMatchObject({ validity: "rerun-required", reason: 'missing tests observation: no GitHub Actions run of "Unit tests"' });
  });

  it("treats a maintainer's latest change request as invalid standards and spec", () => {
    const result = check({ reviews: [review({ id: 1 }), review({ id: 2, state: "CHANGES_REQUESTED" }), review({ id: 3, login: "hill" })] });
    expect(result.decision("standards")).toMatchObject({ validity: "invalid", reason: "standards observation failed: @ted requested changes", produce: APPROVE });
    expect(result.decision("spec")).toMatchObject({ validity: "invalid", reason: "spec observation failed: @ted requested changes" });
  });

  it("accepts an approval that follows the same reviewer's change request", () => {
    const result = check({ reviews: [review({ id: 1, state: "CHANGES_REQUESTED" }), review({ id: 2 })] });
    expect(result.code).toBe(0);
  });
});

describe("acceptance: re-runs", () => {
  it("passes when a failed run is re-run successfully in the same suite", () => {
    const others = CHECK_NAMES.filter((name) => name !== "Unit tests").map((name) => checkRun(name));
    const result = check({ checkRuns: [...others, checkRun("Unit tests", { id: 1, conclusion: "failure" }), checkRun("Unit tests", { id: 2 })] });
    expect(result.decision("tests").validity).toBe("observed-current");
  });

  it("fails with a rerun hint when any suite failed", () => {
    const others = CHECK_NAMES.filter((name) => name !== "Unit tests").map((name) => checkRun(name));
    const result = check({ checkRuns: [...others, checkRun("Unit tests", { id: 1, checkSuiteId: 1 }), checkRun("Unit tests", { id: 2, checkSuiteId: 2, runId: 901, conclusion: "failure" })] });
    expect(result.decision("tests")).toMatchObject({
      validity: "invalid",
      reason: 'tests observation failed: failing: "Unit tests" (failure)',
      produce: 'push the head so CI runs "Unit tests", or rerun it: gh run rerun 901 --failed --repo o/r',
    });
  });
});

describe("acceptance: packet store", () => {
  it("stores immutable packets and rejects replacement", () => {
    const root = mkdtempSync(join(tmpdir(), "readiness-store-")); const store = new FilePacketStore(root);
    expect(invoke(["publish", "--repo", "o/r", "--pr", "7", "--takeover", "true"], fakeForge(), store).code).toBe(0);
    const key = `o/r#7:${HEAD}:${sha256(fakeForgeBody())}`;
    const first = store.get(key)!;
    expect(() => store.putIfAbsent(key, { ...first, bodyDigest: "tampered", digest: "bad" })).toThrow();
    expect(readFileSync(join(root, `${sha256(key)}.json`), "utf8")).toContain(first.digest);
  });
});
function fakeForgeBody(): string { return fakeForge().readPullRequest("o/r", 7).body!; }

describe("acceptance: GitHub-native authority", () => {
  it("keeps an older change request from an eligible reviewer after another reviewer approves the head", () => {
    const result = check({ reviews: [review({ id: 1, state: "CHANGES_REQUESTED", commitId: "c".repeat(40) }), review({ id: 2, login: "hill" })] });
    expect(result.code).toBe(1);
    expect(result.decision("standards")).toMatchObject({ validity: "invalid", reason: "standards observation failed: @ted requested changes" });
  });

  it("ignores a human author's approval of their own PR", () => {
    const result = check({ author: "tlmader", reviews: [review({ login: "tlmader", permission: "admin" })] });
    expect(result.code).toBe(1);
    expect(result.decision("standards")).toMatchObject({ validity: "rerun-required", reason: "missing standards observation: no maintainer approval at 0123456789ab; review by PR author @tlmader ignored" });
  });

  it("requires GitHub's own review decision to be APPROVED", () => {
    const result = check({ reviewDecision: "REVIEW_REQUIRED" });
    expect(result.code).toBe(1);
    expect(result.decision("standards")).toMatchObject({ validity: "rerun-required", reason: "missing standards observation: GitHub review decision is REVIEW_REQUIRED, not APPROVED" });
  });

  it("fails closed when the repository requires no approving review", () => {
    const result = check({ reviewDecision: "none" });
    expect(result.code).toBe(1);
    expect(result.decision("spec")).toMatchObject({ validity: "rerun-required", reason: "missing spec observation: GitHub computes no review decision, so the ruleset requires no approving review" });
  });

  it("no longer posts a readiness status", () => {
    const result = invoke(["gate", "--repo", "o/r", "--pr", "7"], fakeForge());
    expect(result.code).toBe(1);
    expect(result.stderr).toBe("pr-readiness: usage: pr-readiness open|check|publish\n");
  });

  it("treats policy-allowed skipped checks as not applicable", () => {
    const skipped = ["Validate pull request", "Check diff hygiene"].map((name) => checkRun(name, { conclusion: "skipped" }));
    const result = check({ checkRuns: [...CHECK_NAMES.filter((name) => !name.startsWith("Validate") && !name.startsWith("Check")).map((name) => checkRun(name)), ...skipped] });
    expect(result.code).toBe(0);
    expect(result.decision("hygiene")).toMatchObject({ validity: "not-applicable" });
  });

  it("fails a policy-allowed check that skipped while its sibling ran", () => {
    const result = check({ checkRuns: [...CHECK_NAMES.filter((name) => name !== "Check diff hygiene").map((name) => checkRun(name)), checkRun("Check diff hygiene", { conclusion: "skipped" })] });
    expect(result.decision("hygiene")).toMatchObject({ validity: "invalid", reason: 'hygiene observation failed: failing: "Check diff hygiene" (skipped)' });
  });

  it("still fails a skipped check the policy does not allow to skip", () => {
    const result = check({ checkRuns: [...CHECK_NAMES.filter((name) => name !== "Unit tests").map((name) => checkRun(name)), checkRun("Unit tests", { conclusion: "skipped" })] });
    expect(result.decision("tests")).toMatchObject({ validity: "invalid", reason: 'tests observation failed: failing: "Unit tests" (skipped)' });
  });

  it("needs no per-head claims in the PR body", () => {
    const result = check({ body: "## Why\n\nClose the self-approval path." });
    expect(result.stderr).toBe("");
    expect(result.code).toBe(0);
  });
});
