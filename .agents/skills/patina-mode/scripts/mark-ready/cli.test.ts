import { describe, expect, it } from "bun:test";
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { parseRecord, run } from "./cli.ts";
import { checkReadiness } from "./state.ts";
import type { ForgeAdapter } from "./types.ts";

const markdown = (status = "resolved", head = "abc123") =>
  `# Local code review\n\nHead: ${head}\n\n## Standards\n\n- Summary: pass\n\n## Spec\n\n- Summary: pass\n\n### Resolution\n\n- standards-1: Dismissed: code smell only\n\nStatus: ${status}\n`;

describe("local review record", () => {
  it("parses the two axes and resolved status", () => {
    const record = parseRecord("review.md", markdown());
    expect(record).toMatchObject({
      head: "abc123",
      status: "resolved",
      dismissedFindings: 1,
    });
    expect(checkReadiness(record, "abc123", true).ok).toBe(true);
  });

  it("rejects missing, stale, open, and non-draft records", () => {
    const record = parseRecord("review.md", markdown("open", "old"));
    expect(checkReadiness(record, "new", true).ok).toBe(false);
    expect(checkReadiness(record, "old", false).errors).toContain(
      "pull request is already ready"
    );
  });

  it("reads every finding and resolution line in each section", () => {
    const record = (resolution: string) =>
      parseRecord(
        "review.md",
        `Head: abc123\n## Standards\n- Finding S1 (soft): first\n- Finding S2 (hard): second\n## Spec\n- Finding P1 (smell): first\n- Finding P2 (hard): second\n### Resolution\nS1: Dismissed: style only\nP1: Dismissed: smell only\n${resolution}Status: resolved\n`
      );
    const open = record("P2: Fixed in def456\n");
    expect(open.findings.map((finding) => finding.id)).toEqual(["S1", "S2", "P1", "P2"]);
    expect(open.blockingFindings).toBe(1);
    expect(checkReadiness(open, "abc123", true).ok).toBe(false);
    const fixed = record("S2: Fixed in def456\nP2: Fixed in def456\n");
    expect(fixed.blockingFindings).toBe(0);
    expect(fixed.dismissedFindings).toBe(2);
    expect(checkReadiness(fixed, "abc123", true).ok).toBe(true);
  });

  it("reads a section that runs to the end of the record", () => {
    const record = parseRecord(
      "review.md",
      "Head: abc123\n## Standards\n- Finding S1 (soft): first\n## Spec\n- Finding P1 (smell): first\n- Finding P2 (hard): last"
    );
    expect(record.findings.map((finding) => finding.id)).toEqual(["S1", "P1", "P2"]);
    expect(record.blockingFindings).toBe(1);
  });

  it("keeps a hard finding blocking when someone dismisses it", () => {
    const record = parseRecord(
      "review.md",
      `Head: abc123\n\n## Standards\n\n- Finding standards-1 (hard): rule break\n\n## Spec\n\n- Summary: pass\n\n### Resolution\n\n- standards-1: Dismissed: accepted anyway\n\nStatus: resolved\n`
    );
    expect(checkReadiness(record, "abc123", true).errors).toContain(
      "hard finding was dismissed: standards-1"
    );
  });

  it("runs the forge only after the current-head record passes", () => {
    const root = mkdtempSync(join(tmpdir(), "patina-code-review-"));
    const path = join(root, "owner_repo", "feature", "abc123.md");
    mkdirSync(join(root, "owner_repo", "feature"), { recursive: true });
    writeFileSync(path, markdown());
    const calls: string[] = [];
    const forge: ForgeAdapter = {
      name: "gh",
      readPullRequest: () => ({
        repository: "owner_repo",
        branch: "feature",
        number: 1,
        head: "abc123",
        isDraft: true,
      }),
      markReady: () => calls.push("ready"),
    };
    const previous = process.env.PATINA_CODE_REVIEW_ROOT;
    process.env.PATINA_CODE_REVIEW_ROOT = root;
    try {
      expect(run(["--pr", "1"], forge)).toBe(0);
    } finally {
      process.env.PATINA_CODE_REVIEW_ROOT = previous;
    }
    expect(calls).toEqual(["ready"]);
  });

  it("accepts the same record through the Origin adapter", () => {
    const root = mkdtempSync(join(tmpdir(), "patina-code-review-origin-"));
    mkdirSync(join(root, "owner_repo", "feature"), { recursive: true });
    writeFileSync(join(root, "owner_repo", "feature", "abc123.md"), markdown());
    const calls: string[] = [];
    const forge: ForgeAdapter = {
      name: "origin",
      readPullRequest: () => ({
        repository: "owner_repo",
        branch: "feature",
        number: 1,
        head: "abc123",
        isDraft: true,
      }),
      markReady: () => calls.push("origin ready"),
    };
    const previous = process.env.PATINA_CODE_REVIEW_ROOT;
    process.env.PATINA_CODE_REVIEW_ROOT = root;
    try {
      expect(run(["--pr", "1"], forge)).toBe(0);
    } finally {
      process.env.PATINA_CODE_REVIEW_ROOT = previous;
    }
    expect(calls).toEqual(["origin ready"]);
  });

  it("accepts an explicit record from standard input", () => {
    const calls: string[] = [];
    const forge: ForgeAdapter = {
      name: "gh",
      readPullRequest: () => ({
        repository: "owner_repo",
        branch: "feature",
        number: 1,
        head: "abc123",
        isDraft: true,
      }),
      markReady: () => calls.push("ready"),
    };
    expect(run(["--pr", "1", "--record", "-"], forge, () => markdown())).toBe(0);
    expect(calls).toEqual(["ready"]);
  });
});
