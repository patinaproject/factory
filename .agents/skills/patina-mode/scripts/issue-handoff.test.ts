import { afterAll, describe, expect, it } from "bun:test";
import { spawnSync } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  readFileSync,
  realpathSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  ISSUE_ENTRY_POINTS,
  ISSUE_ROUTE_ENTRYPOINTS,
  PROTECTED_OPERATIONS,
  acceptedPlaybooks,
  decideContinuation,
  type CheckoutSnapshot,
  type RouteRequest,
} from "./issue-handoff.ts";

const checkout: CheckoutSnapshot = {
  branch: "435-canonical",
  head: "0123456789abcdef",
  worktreePath: "/worktrees/issue-435",
};
const playbooks = new Set(["refactoring", "investigation", "figure-it-out"]);

function request(
  entry: (typeof ISSUE_ENTRY_POINTS)[number],
  operation: (typeof PROTECTED_OPERATIONS)[number],
  playbook: string | null = "refactoring"
): RouteRequest {
  return {
    entry,
    operation,
    ...(playbook === null ? {} : { playbook }),
    preflight: {
      kind: "passed",
      issue: {
        id: "#435",
        provider: "github",
        url: "https://github.com/patinaproject/skills/issues/435",
      },
      endingBranch: checkout.branch,
      worktreePath: checkout.worktreePath,
    },
  };
}

describe("issue handoff policy", () => {
  it("defines both issue-linked entries and every protected operation", () => {
    expect(ISSUE_ENTRY_POINTS).toEqual(["direct", "session-pickup"]);
    expect(ISSUE_ROUTE_ENTRYPOINTS).toEqual({
      direct: "issue-routes/direct.ts",
      "session-pickup": "issue-routes/session-pickup.ts",
    });
    expect(PROTECTED_OPERATIONS).toEqual([
      "branch-change",
      "edit",
      "commit",
      "pull-request-change",
      "operational-run",
    ]);
  });

  for (const entry of ISSUE_ENTRY_POINTS) {
    for (const operation of PROTECTED_OPERATIONS) {
      it(`refuses ${entry} ${operation} when the preflight is missing`, () => {
        expect(
          decideContinuation({ entry, operation }, checkout, playbooks)
        ).toEqual({
          kind: "stop",
          reason: "missing-preflight",
          blocked: PROTECTED_OPERATIONS,
        });
      });

      it(`allows ${entry} ${operation} after a matching pass`, () => {
        expect(
          decideContinuation(request(entry, operation), checkout, playbooks)
        ).toEqual({
          kind: "continue",
          issue: "#435",
        });
      });

      it(`refuses ${entry} ${operation} when the playbook is missing`, () => {
        expect(
          decideContinuation(
            request(entry, operation, null),
            checkout,
            playbooks
          )
        ).toEqual({
          kind: "stop",
          reason: "missing-playbook",
          blocked: PROTECTED_OPERATIONS,
        });
      });
    }
  }

  for (const gate of [
    "completed-issue",
    "blocker",
    "dirty-worktree",
    "branch-owned-elsewhere",
  ]) {
    it(`refuses a failed ${gate} gate`, () => {
      expect(
        decideContinuation(
          {
            entry: "direct",
            operation: "edit",
            preflight: {
              kind: "failed",
              gate,
              issue: "#435",
            },
          },
          checkout,
          playbooks
        )
      ).toEqual({
        kind: "stop",
        reason: "preflight-failed",
        blocked: PROTECTED_OPERATIONS,
      });
    });
  }

  it("treats no-issue as a terminal routing result", () => {
    expect(
      decideContinuation(
        {
          entry: "direct",
          operation: "edit",
          preflight: { kind: "no-issue" },
        },
        checkout,
        playbooks
      )
    ).toEqual({
      kind: "stop",
      reason: "no-issue",
      blocked: PROTECTED_OPERATIONS,
    });
  });

  it("refuses a pass from a non-canonical branch", () => {
    expect(
      decideContinuation(
        request("direct", "edit", null),
        { ...checkout, branch: "t3code/generated" },
        playbooks
      )
    ).toEqual({
      kind: "stop",
      reason: "stale-checkout",
      blocked: PROTECTED_OPERATIONS,
    });
  });

  it("refuses a pass from another physical worktree", () => {
    expect(
      decideContinuation(
        request("session-pickup", "commit", null),
        { ...checkout, worktreePath: "/worktrees/other" },
        playbooks
      )
    ).toEqual({
      kind: "stop",
      reason: "stale-checkout",
      blocked: PROTECTED_OPERATIONS,
    });
  });

  it("refuses a playbook outside the accepted set", () => {
    expect(
      decideContinuation(
        request("direct", "edit", "not-a-playbook"),
        checkout,
        playbooks
      )
    ).toEqual({
      kind: "stop",
      reason: "unknown-playbook",
      blocked: PROTECTED_OPERATIONS,
    });
  });

  for (const operation of ["edit", "commit"] as const) {
    it(`refuses ${operation} under a read-only playbook`, () => {
      expect(
        decideContinuation(
          request("direct", operation, "investigation"),
          checkout,
          playbooks
        )
      ).toEqual({
        kind: "stop",
        reason: "playbook-not-allowed",
        blocked: PROTECTED_OPERATIONS,
      });
    });
  }

  it("allows an operational run under a read-only playbook", () => {
    expect(
      decideContinuation(
        request("direct", "operational-run", "investigation"),
        checkout,
        playbooks
      )
    ).toEqual({ kind: "continue", issue: "#435" });
  });

  it("accepts bundled stems, project stems, and figure-it-out", () => {
    const accepted = acceptedPlaybooks(checkout);
    expect(accepted.has("refactoring")).toBe(true);
    expect(accepted.has("session-pickup")).toBe(true);
    expect(accepted.has("figure-it-out")).toBe(true);
    expect(accepted.has("not-a-playbook")).toBe(false);
  });
});

describe("issue route entries", () => {
  const entryPoints = {
    direct: join(import.meta.dir, ISSUE_ROUTE_ENTRYPOINTS.direct),
    "session-pickup": join(
      import.meta.dir,
      ISSUE_ROUTE_ENTRYPOINTS["session-pickup"]
    ),
  } as const;
  const receiptRoots: string[] = [];
  const checkoutRoots: string[] = [];

  afterAll(() => {
    for (const path of receiptRoots) {
      rmSync(path, { recursive: true });
    }
    for (const path of checkoutRoots) {
      rmSync(path, { recursive: true });
    }
  });

  function git(cwd: string, ...args: string[]): string {
    const result = Bun.spawnSync(["git", ...args], {
      cwd,
    });
    expect(result.exitCode).toBe(0);
    return result.stdout.toString().trim();
  }

  async function makeCheckout(): Promise<string> {
    const path = await mkdtemp(join(tmpdir(), "issue-handoff-checkout-"));
    checkoutRoots.push(path);
    git(path, "init", "--initial-branch=435-canonical");
    git(path, "config", "user.email", "fixture@example.com");
    git(path, "config", "user.name", "Fixture");
    git(path, "commit", "--allow-empty", "--message=fixture");
    return path;
  }

  async function passedRoute(
    checkoutPath: string,
    fields: Record<string, unknown>
  ) {
    return {
      ...fields,
      preflight: {
        kind: "passed",
        issue: {
          id: "#435",
          provider: "github",
          url: "https://github.com/patinaproject/skills/issues/435",
        },
        endingBranch: git(checkoutPath, "branch", "--show-current"),
        worktreePath: realpathSync(
          git(checkoutPath, "rev-parse", "--show-toplevel")
        ),
      },
    };
  }

  async function run(
    entry: keyof typeof entryPoints,
    input: unknown,
    cwd = import.meta.dir
  ) {
    const receiptRoot = await mkdtemp(join(tmpdir(), "issue-handoff-test-"));
    receiptRoots.push(receiptRoot);
    const result = spawnSync(process.execPath, [entryPoints[entry]], {
      cwd,
      env: { ...process.env, PATINA_ISSUE_RECEIPT_DIR: receiptRoot },
      input: JSON.stringify(input),
      encoding: "utf8",
    });
    return {
      exitCode: result.status,
      stderr: result.stderr,
      stdout: result.stdout,
    };
  }

  for (const entry of ISSUE_ENTRY_POINTS) {
    it(`refuses the ${entry} entry with no working-on-issues result`, async () => {
      const result = await run(entry, { operation: "edit" });

      expect(result.exitCode).toBe(2);
      const output = JSON.parse(result.stdout);
      expect(output.kind).toBe("stop");
      expect(output.reason).toBe("missing-preflight");
      expect(existsSync(output.receiptPath)).toBe(true);
      const receipt = JSON.parse(readFileSync(output.receiptPath, "utf8"));
      expect(receipt.request.entry).toBe(entry);
    });

    it(`routes the ${entry} entry only after a matching pass`, async () => {
      const checkoutPath = await makeCheckout();
      const result = await run(
        entry,
        await passedRoute(checkoutPath, {
          operation: "operational-run",
          playbook: "babysit",
        }),
        checkoutPath
      );

      expect(result.exitCode).toBe(0);
      const output = JSON.parse(result.stdout);
      expect(output.kind).toBe("continue");
      expect(existsSync(output.receiptPath)).toBe(true);
      const receipt = JSON.parse(readFileSync(output.receiptPath, "utf8"));
      expect(receipt.request.entry).toBe(entry);
      expect(receipt.decision.kind).toBe("continue");
    });

    it(`refuses the ${entry} entry when an edit names no playbook`, async () => {
      const checkoutPath = await makeCheckout();
      const result = await run(
        entry,
        await passedRoute(checkoutPath, { operation: "edit" }),
        checkoutPath
      );

      expect(result.exitCode).toBe(2);
      const output = JSON.parse(result.stdout);
      expect(output.reason).toBe("missing-playbook");
      expect(existsSync(output.receiptPath)).toBe(true);
    });
  }

  it("records the playbook in a version 2 receipt", async () => {
    const checkoutPath = await makeCheckout();
    const result = await run(
      "direct",
      await passedRoute(checkoutPath, {
        operation: "edit",
        playbook: "refactoring",
      }),
      checkoutPath
    );

    expect(result.exitCode).toBe(0);
    const output = JSON.parse(result.stdout);
    expect(output.kind).toBe("continue");
    const receipt = JSON.parse(readFileSync(output.receiptPath, "utf8"));
    expect(receipt.schemaVersion).toBe(2);
    expect(receipt.request.playbook).toBe("refactoring");
  });

  it("accepts a project playbook from the checkout", async () => {
    const checkoutPath = await makeCheckout();
    mkdirSync(join(checkoutPath, ".agents", "playbooks"), { recursive: true });
    writeFileSync(
      join(checkoutPath, ".agents", "playbooks", "release-train.md"),
      "---\nextends:\nwhen: Cutting a release.\n---\n"
    );
    const result = await run(
      "direct",
      await passedRoute(checkoutPath, {
        operation: "edit",
        playbook: "release-train",
      }),
      checkoutPath
    );

    expect(result.exitCode).toBe(0);
    expect(JSON.parse(result.stdout).kind).toBe("continue");
  });

  it("rejects a playbook that is not a string", async () => {
    const checkoutPath = await makeCheckout();
    const result = await run(
      "direct",
      await passedRoute(checkoutPath, { operation: "edit", playbook: 7 }),
      checkoutPath
    );

    expect(result.exitCode).toBe(1);
    expect(result.stderr).toBe("playbook must be a non-empty string\n");
  });

  it("persists an explicit no-issue receipt while refusing the route", async () => {
    const result = await run("direct", {
      operation: "pull-request-change",
      preflight: { kind: "no-issue" },
    });

    expect(result.exitCode).toBe(2);
    const output = JSON.parse(result.stdout);
    expect(output.reason).toBe("no-issue");
    const receipt = JSON.parse(readFileSync(output.receiptPath, "utf8"));
    expect(receipt.request.preflight).toEqual({ kind: "no-issue" });
  });
});
