import { describe, expect, it } from "bun:test";
import { chmodSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { GhForge, parseReviewDecision } from "./adapters.ts";

const review = (id: number, login: string, type: string) => JSON.stringify({ id, user: { login, type }, author_association: "CONTRIBUTOR", state: "APPROVED", commit_id: "abc", submitted_at: "2026-10-01T00:00:00Z", html_url: `https://github.com/o/r/pull/7#pullrequestreview-${id}` });

function readWithStubGh(): { permissions: Record<string, string>; calls: string[] } {
  const dir = mkdtempSync(join(tmpdir(), "stub-gh-"));
  const script = `#!/bin/sh
echo "$*" >> "${dir}/calls"
case "$*" in
  *check-runs*) exit 0 ;;
  *reviewDecision*) echo '{"reviewDecision":"APPROVED"}' ;;
  *pulls/7/reviews*) printf '%s\\n' '${review(1, "tlmader", "User")}' '${review(2, "tlmader", "User")}' '${review(3, "stranger", "User")}' '${review(4, "renovate[bot]", "Bot")}' '${review(5, "author", "User")}' ;;
  *pulls/7) echo '{"user":{"login":"author"}}' ;;
  *collaborators/tlmader/permission) echo '{"permission":"admin"}' ;;
  *collaborators/stranger/permission) echo "gh: Not Found (HTTP 404)" >&2; exit 1 ;;
  *) echo "unexpected: $*" >&2; exit 2 ;;
esac
`;
  writeFileSync(join(dir, "gh"), script);
  chmodSync(join(dir, "gh"), 0o755);
  const previous = process.env.PATH;
  process.env.PATH = `${dir}:${previous}`;
  try {
    const evidence = new GhForge("gh").readEvidence("o/r", 7, "abc");
    const calls = readFileSync(join(dir, "calls"), "utf8").split("\n").filter(Boolean);
    return { permissions: Object.fromEntries(evidence.reviews.map((r) => [`${r.id}:${r.login}`, r.permission])), calls };
  } finally {
    process.env.PATH = previous;
  }
}

describe("GhForge.readEvidence reviewer permissions", () => {
  it("resolves each eligible reviewer once and maps a 404 to none", () => {
    const { permissions, calls } = readWithStubGh();
    expect(permissions).toEqual({ "1:tlmader": "admin", "2:tlmader": "admin", "3:stranger": "none", "4:renovate[bot]": "none", "5:author": "none" });
    expect(calls.filter((call) => call.includes("collaborators/"))).toEqual(["api repos/o/r/collaborators/tlmader/permission", "api repos/o/r/collaborators/stranger/permission"]);
  });
});

describe("parseReviewDecision", () => {
  it("maps GitHub's empty decision to none and rejects an unknown value", () => {
    expect(parseReviewDecision("")).toBe("none");
    expect(parseReviewDecision(null)).toBe("none");
    expect(parseReviewDecision("APPROVED")).toBe("APPROVED");
    expect(() => parseReviewDecision("MAYBE")).toThrow("forge returned an unknown review decision: MAYBE");
  });
});

describe("GhForge.readPolicy", () => {
  it("reads the policy from the default branch by omitting the ref", () => {
    const dir = mkdtempSync(join(tmpdir(), "stub-gh-"));
    const content = Buffer.from('{"version":1,"obligations":{}}').toString("base64");
    writeFileSync(join(dir, "gh"), `#!/bin/sh\necho "$*" >> "${dir}/calls"\necho '{"encoding":"base64","content":"${content}"}'\n`);
    chmodSync(join(dir, "gh"), 0o755);
    const previous = process.env.PATH;
    process.env.PATH = `${dir}:${previous}`;
    try {
      expect(new GhForge("gh").readPolicy("o/r")).toBe('{"version":1,"obligations":{}}');
    } finally {
      process.env.PATH = previous;
    }
    expect(readFileSync(join(dir, "calls"), "utf8")).toBe("api repos/o/r/contents/.github/pr-readiness.json\n");
  });
});
