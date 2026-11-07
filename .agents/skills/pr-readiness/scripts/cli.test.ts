import { describe, expect, it } from "bun:test";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { run } from "./cli.ts";
import { MemoryPacketStore } from "./adapters.ts";
import { AUTO_MERGE_POLICY, CHECK_NAMES, HEAD, POLICY, checkRun, fakeForge, invoke, review, type FakeForge } from "./fake-forge.ts";
import type { EvaluationReport } from "./evaluator.ts";

const report = (stdout: string): EvaluationReport => JSON.parse(stdout) as EvaluationReport;

describe("pr-readiness check", () => {
  it("exits 0 when ready without marking the PR ready", () => {
    const forge = fakeForge();
    const result = invoke(["check", "--repo", "o/r", "--pr", "7"], forge);
    expect(result.code).toBe(0);
    expect(report(result.stdout).ready).toBe(true);
    expect(forge.markReadyCalls).toEqual([]);
  });
  it("fails with the policy parse error for a malformed policy file", () => {
    const result = invoke(["check", "--repo", "o/r", "--pr", "7"], fakeForge({ policy: "{ not json" }));
    expect(result.code).toBe(1);
    expect(result.stderr).toStartWith("pr-readiness: .github/pr-readiness.json is not valid JSON");
  });
  it("fails naming an unknown obligation in the policy file", () => {
    const result = invoke(["check", "--repo", "o/r", "--pr", "7"], fakeForge({ policy: JSON.stringify({ version: 1, obligations: { vibes: { producer: "approval" } } }) }));
    expect(result.code).toBe(1);
    expect(result.stderr).toBe("pr-readiness: .github/pr-readiness.json names unknown obligation: vibes\n");
  });
  it("requires approval for every obligation when the base has no policy file", () => {
    const result = invoke(["check", "--repo", "o/r", "--pr", "7"], fakeForge({ policy: undefined, reviews: [] }));
    expect(result.code).toBe(1);
    const produce = `a maintainer other than @patinaproject-goodyear[bot] reviews 0123456789ab and approves it: gh api repos/o/r/pulls/7/reviews -f commit_id=${HEAD} -f event=APPROVE`;
    expect(report(result.stdout).decisions.map((d) => [d.obligation, d.validity, d.produce])).toEqual([
      ["standards", "rerun-required", produce], ["spec", "rerun-required", produce], ["hygiene", "rerun-required", produce],
      ["tests", "rerun-required", produce], ["lint", "rerun-required", produce], ["behavior", "rerun-required", produce],
    ]);
  });
});

describe("pr-readiness publish", () => {
  it("rejects a head race without forge mutation", () => {
    const forge = fakeForge({ heads: [HEAD, "f".repeat(40)] });
    const store = new MemoryPacketStore();
    const result = invoke(["publish", "--repo", "o/r", "--pr", "7", "--takeover", "true"], forge, store);
    expect(result.code).toBe(1);
    expect(forge.markReadyCalls).toEqual([]);
    expect(store.packets.size).toBe(0);
  });
  it("is idempotent for an already-ready PR", () => {
    const forge = fakeForge({ isDraft: false });
    const result = invoke(["publish", "--repo", "o/r", "--pr", "7"], forge);
    expect(result.code).toBe(0);
    expect(result.stdout).toBe(`PR #7 is already ready at ${HEAD}\n`);
  });
  it("refuses an unready candidate", () => {
    const forge = fakeForge({ checkRuns: [checkRun("Unit tests", { conclusion: "failure" })], reviews: [review({ state: "CHANGES_REQUESTED" })] });
    const result = invoke(["publish", "--repo", "o/r", "--pr", "7", "--takeover", "true"], forge);
    expect(result.code).toBe(1);
    expect(forge.markReadyCalls).toEqual([]);
  });
});

describe("pr-readiness run", () => {
  it("returns 1 through the error path when no command is given", () => {
    const err = process.stderr.write.bind(process.stderr); let stderr = "";
    process.stderr.write = ((chunk: string) => { stderr += chunk; return true; }) as typeof process.stderr.write;
    const store = process.env.PATINA_READINESS_STORE; process.env.PATINA_READINESS_STORE = mkdtempSync(join(tmpdir(), "readiness-run-"));
    try { expect(run([])).toBe(1); } finally { process.stderr.write = err; if (store === undefined) delete process.env.PATINA_READINESS_STORE; else process.env.PATINA_READINESS_STORE = store; }
    expect(stderr).toBe("pr-readiness: usage: pr-readiness open|check|publish\n");
  });
});

describe("pr-readiness publish auto-merge", () => {
  const publish = (forge: FakeForge) => invoke(["publish", "--repo", "o/r", "--pr", "7", "--takeover", "true"], forge);
  it("arms one squash auto-merge at the published head after the ready transition", () => {
    const forge = fakeForge({ policy: AUTO_MERGE_POLICY });
    const result = publish(forge);
    expect(result.code).toBe(0);
    expect(forge.markReadyCalls).toEqual([HEAD]);
    expect(forge.enableAutoMergeCalls).toEqual([{ head: HEAD, method: "SQUASH" }]);
    expect(result.stdout).toBe(`PR #7 is ready at ${HEAD}\nauto-merge (squash) armed at ${HEAD}\n`);
  });
  it("arms nothing and reads no rules when autoMerge is false or missing", () => {
    for (const policy of [JSON.stringify({ ...JSON.parse(POLICY), autoMerge: false }), POLICY]) {
      const forge = fakeForge({ policy });
      const result = publish(forge);
      expect(result.code).toBe(0);
      expect(forge.enableAutoMergeCalls).toEqual([]);
      expect(forge.readRequiredChecksCalls).toEqual([]);
      expect(forge.readAutoMergeStateCalls).toEqual([]);
      expect(result.stdout).toBe(`PR #7 is ready at ${HEAD}\n`);
    }
  });
  it("skips a stacked child whose base is not the default branch", () => {
    const forge = fakeForge({ policy: AUTO_MERGE_POLICY, baseRef: "feature/parent" });
    const result = publish(forge);
    expect(result.code).toBe(0);
    expect(forge.markReadyCalls).toEqual([HEAD]);
    expect(forge.enableAutoMergeCalls).toEqual([]);
    expect(forge.readRequiredChecksCalls).toEqual([]);
    expect(result.stdout).toContain("auto-merge skipped: base feature/parent is not the default branch main\n");
  });
  it("skips a diff that touches workflows or the policy file, naming the paths", () => {
    const forge = fakeForge({ policy: AUTO_MERGE_POLICY, changedPaths: ["src/a.ts", ".github/workflows/x.yml", ".github/pr-readiness.json"] });
    const result = publish(forge);
    expect(result.code).toBe(0);
    expect(forge.enableAutoMergeCalls).toEqual([]);
    expect(result.stdout).toContain("auto-merge skipped: the diff touches .github/workflows/x.yml, .github/pr-readiness.json\n");
  });
  it("fails closed without marking ready when the default branch is unknown", () => {
    const forge = fakeForge({ policy: AUTO_MERGE_POLICY, defaultBranch: null });
    const result = publish(forge);
    expect(result.code).toBe(1);
    expect(result.stderr).toContain("default branch");
    expect(forge.markReadyCalls).toEqual([]);
  });
  it("refuses, leaving the PR untouched, when the base does not require a policy check", () => {
    const store = new MemoryPacketStore();
    const forge = fakeForge({ policy: AUTO_MERGE_POLICY, requiredChecks: CHECK_NAMES.filter((name) => name !== "Unit tests" && name !== "Lint Markdown") });
    const result = invoke(["publish", "--repo", "o/r", "--pr", "7", "--takeover", "true"], forge, store);
    expect(result.code).toBe(1);
    expect(result.stderr).toBe('pr-readiness: cannot arm auto-merge: main does not require "Unit tests", "Lint Markdown"\n');
    expect(forge.markReadyCalls).toEqual([]);
    expect(forge.enableAutoMergeCalls).toEqual([]);
    expect(store.packets.size).toBe(0);
  });
  it("fails closed when the rules read throws", () => {
    const forge = fakeForge({ policy: AUTO_MERGE_POLICY, requiredChecksError: "HTTP 502" });
    const result = publish(forge);
    expect(result.code).toBe(1);
    expect(result.stderr).toBe("pr-readiness: HTTP 502\n");
    expect(forge.markReadyCalls).toEqual([]);
    expect(forge.enableAutoMergeCalls).toEqual([]);
  });
  it("fails closed before the ready transition when the arm-state read throws", () => {
    const forge = fakeForge({ policy: AUTO_MERGE_POLICY, autoMergeStateError: "forge graphql failed" });
    const result = publish(forge);
    expect(result.code).toBe(1);
    expect(result.stderr).toBe("pr-readiness: forge graphql failed\n");
    expect(forge.readAutoMergeStateCalls).toEqual([7]);
    expect(forge.markReadyCalls).toEqual([]);
    expect(forge.enableAutoMergeCalls).toEqual([]);
  });
  it("does nothing when already armed with squash at the published head", () => {
    const forge = fakeForge({ policy: AUTO_MERGE_POLICY, isDraft: false, armed: { mergeMethod: "SQUASH", head: HEAD } });
    const result = publish(forge);
    expect(result.code).toBe(0);
    expect(forge.enableAutoMergeCalls).toEqual([]);
    expect(result.stdout).toBe(`PR #7 is already ready at ${HEAD}\nauto-merge already armed (squash) at ${HEAD}\n`);
  });
  it("replaces an arm at an older head, an unknown head, or another method", () => {
    for (const armed of [{ mergeMethod: "SQUASH", head: "e".repeat(40) }, { mergeMethod: "SQUASH" }, { mergeMethod: "MERGE", head: HEAD }]) {
      const forge = fakeForge({ policy: AUTO_MERGE_POLICY, armed });
      expect(publish(forge).code).toBe(0);
      expect(forge.enableAutoMergeCalls).toEqual([{ head: HEAD, method: "SQUASH" }]);
    }
  });
  it("reports a ready PR without auto-merge when the forge refuses to arm", () => {
    const forge = fakeForge({ policy: AUTO_MERGE_POLICY, enableAutoMergeError: "Pull request is in clean status" });
    const result = publish(forge);
    expect(result.code).toBe(1);
    expect(forge.markReadyCalls).toEqual([HEAD]);
    expect(result.stderr).toBe("pr-readiness: PR #7 is ready but auto-merge is not on: Pull request is in clean status\n");
  });
});
