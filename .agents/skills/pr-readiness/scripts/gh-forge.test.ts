import { describe, expect, it } from "bun:test";
import { chmodSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { GhForge, armedHeadFromTimeline, parseChangedPaths, parseReviewDecision, requiredContexts } from "./adapters.ts";

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

describe("armedHeadFromTimeline", () => {
  const commit = (oid: string) => ({ __typename: "PullRequestCommit", commit: { oid } });
  const enabled = { __typename: "AutoMergeEnabledEvent", createdAt: "2026-11-01T00:00:00Z" };
  it("takes the head current at the last enable event, following force pushes", () => {
    expect(armedHeadFromTimeline([commit("a"), commit("b"), enabled, { __typename: "HeadRefForcePushedEvent", afterCommit: { oid: "c" } }, commit("d"), enabled, commit("e")])).toBe("d");
  });
  it("is undefined when never enabled or the head is unknown at the event", () => {
    expect(armedHeadFromTimeline([commit("a")])).toBeUndefined();
    expect(armedHeadFromTimeline([enabled, commit("a")])).toBeUndefined();
  });
});

describe("requiredContexts", () => {
  it("collects contexts from every required_status_checks rule only", () => {
    const rules = [
      { type: "deletion" },
      { type: "required_status_checks", parameters: { required_status_checks: [{ context: "A" }, { context: "B" }] } },
      { type: "required_status_checks", parameters: { required_status_checks: [{ context: "B" }, { context: "C" }] } },
      { type: "pull_request", parameters: { required_status_checks: [{ context: "Z" }] } },
    ];
    expect(requiredContexts(rules)).toEqual(["A", "B", "C"]);
  });
});

describe("GhForge.enableAutoMerge", () => {
  function stub(armed: boolean): string[] {
    const dir = mkdtempSync(join(tmpdir(), "stub-gh-"));
    const pr = JSON.stringify({ data: { repository: { defaultBranchRef: { name: "main" }, pullRequest: { id: "PR_1", autoMergeRequest: armed ? { mergeMethod: "MERGE" } : null, timelineItems: { nodes: [] } } } } });
    writeFileSync(join(dir, "gh"), `#!/bin/sh\necho "$*" >> "${dir}/calls"\ncase "$*" in\n  *mutation*) echo '{"data":{}}' ;;\n  *) echo '${pr}' ;;\nesac\n`);
    chmodSync(join(dir, "gh"), 0o755);
    const previous = process.env.PATH;
    process.env.PATH = `${dir}:${previous}`;
    try { new GhForge("gh").enableAutoMerge("o/r", 7, "abc"); } finally { process.env.PATH = previous; }
    return readFileSync(join(dir, "calls"), "utf8").split("\n").filter((call) => call.includes("mutation"));
  }
  it("enables squash pinned to the head, disabling an existing request first", () => {
    const fresh = stub(false);
    expect(fresh).toHaveLength(1);
    expect(fresh[0]).toContain("enablePullRequestAutoMerge(input:{pullRequestId:$id,mergeMethod:SQUASH,expectedHeadOid:$head})");
    expect(fresh[0]).toContain("head=abc");
    const replaced = stub(true);
    expect(replaced).toHaveLength(2);
    expect(replaced[0]).toContain("disablePullRequestAutoMerge");
    expect(replaced[1]).toContain("enablePullRequestAutoMerge");
  });
});

describe("GhForge.readAutoMergeState", () => {
  it("returns the default branch and the armed request, and malformed data throws", () => {
    const dir = mkdtempSync(join(tmpdir(), "stub-gh-"));
    const enabled = { __typename: "AutoMergeEnabledEvent", createdAt: "2026-11-01T00:00:00Z" };
    const nodes = [{ __typename: "PullRequestCommit", commit: { oid: "abc" } }, enabled];
    const pr = JSON.stringify({ data: { repository: { defaultBranchRef: { name: "main" }, pullRequest: { id: "PR_1", autoMergeRequest: { mergeMethod: "SQUASH" }, timelineItems: { nodes } } } } });
    writeFileSync(join(dir, "gh"), `#!/bin/sh\ncase "$*" in\n  *pulls/7/files*) echo '{"filename":"a.ts"}' ;;\n  *malformed*) echo '{"data":{"repository":null}}' ;;\n  *) echo '${pr}' ;;\nesac\n`);
    chmodSync(join(dir, "gh"), 0o755);
    const previous = process.env.PATH;
    process.env.PATH = `${dir}:${previous}`;
    try {
      expect(new GhForge("gh").readAutoMergeState("o/r", 7)).toEqual({ defaultBranch: "main", changedPaths: ["a.ts"], armed: { mergeMethod: "SQUASH", head: "abc" } });
      expect(() => new GhForge("gh").readAutoMergeState("malformed/r", 7)).toThrow("malformed pull request arm state");
    } finally { process.env.PATH = previous; }
  });
});

describe("parseChangedPaths", () => {
  it("collects filenames and rename sources, keeps non-ASCII paths verbatim, and rejects rows without a filename", () => {
    const rows = [
      { filename: "src/a.ts", status: "modified" },
      { filename: "docs/new.md", previous_filename: ".github/workflows/old.yml", status: "renamed" },
      { filename: ".github/workflows/\u00e9.yml", status: "added" },
      { filename: "src/a.ts", status: "modified" },
    ];
    expect(parseChangedPaths(rows)).toEqual(["src/a.ts", "docs/new.md", ".github/workflows/old.yml", ".github/workflows/\u00e9.yml"]);
    expect(() => parseChangedPaths([{ status: "added" }])).toThrow("malformed changed file: filename");
    expect(() => parseChangedPaths(["x"])).toThrow("malformed changed file");
  });
});
