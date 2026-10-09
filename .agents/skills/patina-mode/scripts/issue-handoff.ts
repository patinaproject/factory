import { createHash } from "node:crypto";
import {
  mkdirSync,
  readFileSync,
  realpathSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { isAbsolute, join } from "node:path";

export const ISSUE_ENTRY_POINTS = ["direct", "session-pickup"] as const;
export const PROTECTED_OPERATIONS = [
  "branch-change",
  "edit",
  "commit",
  "pull-request-change",
  "operational-run",
] as const;

export type IssueEntryPoint = (typeof ISSUE_ENTRY_POINTS)[number];
type ProtectedOperation = (typeof PROTECTED_OPERATIONS)[number];
type Provider = "github" | "linear" | "other";

export const ISSUE_ROUTE_ENTRYPOINTS = {
  direct: "issue-routes/direct.ts",
  "session-pickup": "issue-routes/session-pickup.ts",
} as const satisfies Readonly<Record<IssueEntryPoint, string>>;

export interface CheckoutSnapshot {
  readonly branch: string;
  readonly head: string;
  readonly worktreePath: string;
}

interface PassedPreflight {
  readonly kind: "passed";
  readonly issue: {
    readonly id: string;
    readonly provider: Provider;
    readonly url: string;
  };
  readonly endingBranch: string;
  readonly worktreePath: string;
}

interface FailedPreflight {
  readonly kind: "failed";
  readonly gate: string;
  readonly issue?: string;
}

interface NoIssuePreflight {
  readonly kind: "no-issue";
}

type WorkingOnIssuesResult =
  | PassedPreflight
  | FailedPreflight
  | NoIssuePreflight;

export interface RouteRequest {
  readonly entry: IssueEntryPoint;
  readonly operation: ProtectedOperation;
  readonly preflight?: WorkingOnIssuesResult;
}

type StopReason =
  | "missing-preflight"
  | "preflight-failed"
  | "no-issue"
  | "stale-checkout";

export type RoutingDecision =
  | { readonly kind: "continue"; readonly issue: string }
  | {
      readonly kind: "stop";
      readonly reason: StopReason;
      readonly blocked: typeof PROTECTED_OPERATIONS;
    };

interface Receipt {
  readonly schemaVersion: 1;
  readonly request: RouteRequest;
  readonly checkout: CheckoutSnapshot;
  readonly decision: RoutingDecision;
}

function stop(reason: StopReason): RoutingDecision {
  return { kind: "stop", reason, blocked: PROTECTED_OPERATIONS };
}

export function decideContinuation(
  request: RouteRequest,
  checkout: CheckoutSnapshot
): RoutingDecision {
  if (request.preflight === undefined) {
    return stop("missing-preflight");
  }

  if (request.preflight.kind === "failed") {
    return stop("preflight-failed");
  }

  if (request.preflight.kind === "no-issue") {
    return stop("no-issue");
  }

  if (
    request.preflight.endingBranch !== checkout.branch ||
    request.preflight.worktreePath !== checkout.worktreePath
  ) {
    return stop("stale-checkout");
  }

  return { kind: "continue", issue: request.preflight.issue.id };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function readString(value: unknown, field: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`${field} must be a non-empty string`);
  }
  return value;
}

function readEnum<const Values extends readonly string[]>(
  value: unknown,
  field: string,
  values: Values
): Values[number] {
  if (typeof value !== "string" || !values.includes(value)) {
    throw new Error(`${field} must be one of ${values.join(", ")}`);
  }
  return value as Values[number];
}

function parsePreflight(value: unknown): WorkingOnIssuesResult {
  if (!isRecord(value)) {
    throw new Error("preflight must be an object");
  }

  const kind = readEnum(value.kind, "preflight.kind", [
    "passed",
    "failed",
    "no-issue",
  ] as const);
  if (kind === "no-issue") {
    return { kind };
  }
  if (kind === "failed") {
    return {
      kind,
      gate: readString(value.gate, "preflight.gate"),
      ...(value.issue === undefined
        ? {}
        : { issue: readString(value.issue, "preflight.issue") }),
    };
  }

  if (!isRecord(value.issue)) {
    throw new Error("preflight.issue must be an object");
  }
  return {
    kind,
    issue: {
      id: readString(value.issue.id, "preflight.issue.id"),
      provider: readEnum(value.issue.provider, "preflight.issue.provider", [
        "github",
        "linear",
        "other",
      ] as const),
      url: readString(value.issue.url, "preflight.issue.url"),
    },
    endingBranch: readString(
      value.endingBranch,
      "preflight.endingBranch"
    ),
    worktreePath: realpathSync(
      readString(value.worktreePath, "preflight.worktreePath")
    ),
  };
}

export function parseRouteInput(
  entry: IssueEntryPoint,
  value: unknown
): RouteRequest {
  if (!isRecord(value)) {
    throw new Error("route request must be an object");
  }
  return {
    entry,
    operation: readEnum(value.operation, "operation", PROTECTED_OPERATIONS),
    ...(value.preflight === undefined
      ? {}
      : { preflight: parsePreflight(value.preflight) }),
  };
}

function runGit(cwd: string, args: readonly string[]): string {
  const result = Bun.spawnSync(["git", ...args], { cwd });
  if (result.exitCode !== 0) {
    const detail = result.stderr.toString().trim();
    throw new Error(`git ${args.join(" ")} failed${detail ? `: ${detail}` : ""}`);
  }
  return result.stdout.toString().trim();
}

export function readCheckout(cwd: string): CheckoutSnapshot {
  return {
    branch: runGit(cwd, ["branch", "--show-current"]),
    head: runGit(cwd, ["rev-parse", "HEAD"]),
    worktreePath: realpathSync(runGit(cwd, ["rev-parse", "--show-toplevel"])),
  };
}

function receiptDirectory(): string {
  const configured = process.env.PATINA_ISSUE_RECEIPT_DIR;
  if (configured === undefined) {
    return join(tmpdir(), "patina-issue-handoffs");
  }
  if (!isAbsolute(configured)) {
    throw new Error("PATINA_ISSUE_RECEIPT_DIR must be an absolute path");
  }
  return configured;
}

function writeReceipt(receipt: Receipt): string {
  const contents = `${JSON.stringify(receipt, null, 2)}\n`;
  const digest = createHash("sha256").update(contents).digest("hex");
  const directory = receiptDirectory();
  const path = join(directory, `${digest}.json`);
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  try {
    writeFileSync(path, contents, { encoding: "utf8", flag: "wx", mode: 0o600 });
  } catch (error) {
    if (
      !isRecord(error) ||
      error.code !== "EEXIST" ||
      readFileSync(path, "utf8") !== contents
    ) {
      throw error;
    }
  }
  return path;
}

export async function runIssueRoute(entry: IssueEntryPoint): Promise<void> {
  try {
    if (process.argv.length !== 2) {
      throw new Error(
        `usage: bun ${ISSUE_ROUTE_ENTRYPOINTS[entry]} < route-request.json`
      );
    }
    const source = await Bun.stdin.text();
    const raw = JSON.parse(source);
    const request = parseRouteInput(entry, raw);
    const checkout = readCheckout(process.cwd());
    const decision = decideContinuation(request, checkout);
    const receiptPath = writeReceipt({
      schemaVersion: 1,
      request,
      checkout,
      decision,
    });
    process.stdout.write(`${JSON.stringify({ ...decision, receiptPath })}\n`);
    if (decision.kind === "stop") {
      process.exitCode = 2;
    }
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    process.stderr.write(`${message}\n`);
    process.exitCode = 1;
  }
}
