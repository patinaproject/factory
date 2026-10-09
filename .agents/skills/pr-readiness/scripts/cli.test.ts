import { describe, expect, it } from "bun:test";
import { operation } from "./cli.ts";
import { MemoryPacketStore, type Forge, type PullRequestState } from "./adapters.ts";

const state = (draft = true, head = "h"): PullRequestState => ({ repository: "o/r", number: 1, base: "main", mergeBase: "m", head, patchId: "p", diffDigest: "d", requirementsDigest: "req", policyDigest: "pol", isDraft: draft });
const forge = (current: PullRequestState, mutate?: () => void): Forge => ({ readPullRequest: () => current, openPullRequest: () => current, markReady: () => mutate?.() });

describe("pr-readiness CLI operations", () => {
  it("check is read-only", () => {
    let ready = 0;
    const result = operation(["check", "--repo", "o/r", "--pr", "1"], { forge: forge(state(), () => ready++), store: new MemoryPacketStore(), observations: () => [] });
    expect(result).toBe(1); expect(ready).toBe(0);
  });
  it("rejects a head race without forge mutation", () => {
    let reads = 0; let ready = 0;
    const initial = state(); const f: Forge = { ...forge(initial, () => ready++), readPullRequest: () => (++reads === 1 ? initial : state(true, "new")) };
    const result = operation(["publish", "--repo", "o/r", "--pr", "1"], { forge: f, store: new MemoryPacketStore(), observations: () => [] });
    expect(result).toBe(1); expect(ready).toBe(0);
  });
  it("is idempotent for an already-ready PR", () => {
    const store = new MemoryPacketStore(); const current = state(false);
    const result = operation(["publish", "--repo", "o/r", "--pr", "1", "--body", ""], { forge: forge(current), store, observations: () => [], rules: [] });
    expect(result).toBe(0);
  });
});
