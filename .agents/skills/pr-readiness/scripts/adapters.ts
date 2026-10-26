import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, openSync, closeSync, writeFileSync, existsSync } from "node:fs";
import { join, resolve } from "node:path";
import { sha256, verifyPacket, type CandidateIdentity, type ReadinessPacket } from "./domain.ts";
import { POLICY_PATH } from "./policy.ts";
import type { CheckRunFact, ForgeEvidence, ReviewDecision, ReviewFact } from "./evidence.ts";

export interface PullRequestState extends Omit<CandidateIdentity, "policyDigest"> { readonly number: number; readonly isDraft: boolean; readonly body?: string; readonly baseRef?: string; readonly headRef?: string; }
export interface Forge {
  readPullRequest(repository: string, number: number): PullRequestState;
  openPullRequest(input: { repository: string; base: string; head: string; draft?: boolean }): PullRequestState;
  markReady(repository: string, number: number, expectedHead: string): void;
  readPolicy(repository: string): string | undefined;
  readEvidence(repository: string, number: number, head: string): ForgeEvidence;
}
export interface PacketStore { get(key: string): ReadinessPacket | undefined; putIfAbsent(key: string, packet: ReadinessPacket): ReadinessPacket; }

export function candidateFromPullRequest(pr: PullRequestState, policyDigest: string): CandidateIdentity {
  return { repository: pr.repository, pullRequest: pr.pullRequest || pr.number, base: pr.base, baseSha: pr.baseSha, baseRef: pr.baseRef, mergeBase: pr.mergeBase, head: pr.head, patchId: pr.patchId, diffDigest: pr.diffDigest, behaviorApplicable: pr.behaviorApplicable, requirementsDigest: pr.requirementsDigest, policyDigest, reviewContextDigest: pr.reviewContextDigest };
}

function jsonOutput(command: string, args: readonly string[]): Record<string, unknown> { const output = execFileSync(command, [...args], { encoding: "utf8" }); const parsed: unknown = JSON.parse(output); if (!parsed || typeof parsed !== "object") throw new Error("forge returned invalid JSON"); return parsed as Record<string, unknown>; }
function jsonLines(command: string, args: readonly string[]): unknown[] {
  return execFileSync(command, [...args], { encoding: "utf8", maxBuffer: 64 * 1024 * 1024 }).split("\n").filter((line) => line.trim()).map((line) => JSON.parse(line) as unknown);
}
function field<T>(row: Record<string, unknown>, key: string, check: (value: unknown) => value is T, kind: string): T {
  const value = row[key];
  if (!check(value)) throw new Error(`forge returned a malformed ${kind}: ${key}`);
  return value;
}
const isString = (value: unknown): value is string => typeof value === "string";
const isId = (value: unknown): value is number => Number.isInteger(value) && (value as number) > 0;
const isNullableString = (value: unknown): value is string | null | undefined => value === null || value === undefined || typeof value === "string";
const isObject = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);
export function parseCheckRun(value: unknown): CheckRunFact {
  if (!isObject(value)) throw new Error("forge returned a malformed check run");
  const app = field(value, "app", isObject, "check run");
  const suite = field(value, "check_suite", isObject, "check run");
  const status = field(value, "status", isString, "check run");
  const completedAt = field(value, "completed_at", isNullableString, "check run") ?? null;
  if (status === "completed" && completedAt === null) throw new Error("forge returned a completed check run without completed_at");
  return {
    id: field(value, "id", isId, "check run"), name: field(value, "name", isString, "check run"), appSlug: field(app, "slug", isString, "check run"),
    status, conclusion: field(value, "conclusion", isNullableString, "check run") ?? null, checkSuiteId: field(suite, "id", isId, "check run"),
    detailsUrl: field(value, "details_url", isNullableString, "check run") ?? "", htmlUrl: field(value, "html_url", isString, "check run"), completedAt,
  };
}
export function parseReview(value: unknown): Omit<ReviewFact, "permission"> {
  if (!isObject(value)) throw new Error("forge returned a malformed review");
  const user = field(value, "user", isObject, "review");
  const state = field(value, "state", isString, "review");
  const submittedAt = field(value, "submitted_at", isNullableString, "review") ?? null;
  if (state !== "PENDING" && submittedAt === null) throw new Error("forge returned a submitted review without submitted_at");
  return {
    id: field(value, "id", isId, "review"), login: field(user, "login", isString, "review"), userType: field(user, "type", isString, "review"),
    state, commitId: field(value, "commit_id", isString, "review"), submittedAt, htmlUrl: field(value, "html_url", isString, "review"),
  };
}
const REVIEW_DECISIONS: readonly string[] = ["APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED"];
export function parseReviewDecision(value: unknown): ReviewDecision {
  if (value === "" || value === null) return "none";
  if (typeof value === "string" && REVIEW_DECISIONS.includes(value)) return value as ReviewDecision;
  throw new Error(`forge returned an unknown review decision: ${String(value)}`);
}
const notFound = (error: unknown): boolean => /HTTP 404/.test(String((error as { stderr?: unknown }).stderr ?? ""));
function git(command: string, args: readonly string[]): string { return execFileSync(command, [...args], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim(); }
function remoteDiff(command: string, repository: string, number: number): string { try { return execFileSync(command, ["pr", "diff", String(number), "--repo", repository, "--patch"], { encoding: "utf8" }); } catch { return ""; } }
function derivePatchId(diff: string): string { if (!diff) return sha256(""); try { const result = execFileSync("git", ["patch-id", "--stable"], { input: diff, encoding: "utf8" }).trim(); return result.split(/\s+/)[0] || sha256(diff); } catch { return sha256(diff); } }

/** Production adapter. It only reads forge metadata and patch text; it never executes PR-head code. */
export class GhForge implements Forge {
  constructor(readonly command: "gh" | "origin" = "gh") {}
  readPullRequest(repository: string, number: number): PullRequestState {
    const row = jsonOutput(this.command, ["pr", "view", String(number), "--repo", repository, "--json", "number,body,isDraft,baseRefName,baseRefOid,headRefName,headRefOid,mergeCommit"]);
    const head = typeof row.headRefOid === "string" ? row.headRefOid : "";
    const baseSha = typeof row.baseRefOid === "string" ? row.baseRefOid : "";
    const baseRef = typeof row.baseRefName === "string" ? row.baseRefName : "";
    const diff = remoteDiff(this.command, repository, number);
    const mergeBase = baseSha && head ? (() => { try { return git("git", ["merge-base", baseSha, head]); } catch { return baseSha; } })() : baseSha;
    const changedPaths = [...diff.matchAll(/^diff --git a\/(.+?) b\/(.+)$/gm)].map((match) => `${match[1]} ${match[2]}`);
    const behaviorApplicable = changedPaths.length === 0 || changedPaths.some((path) => !/(^|\/)(docs?|documentation)\/|\.md$|\.mdx$|\.txt$/i.test(path));
    return { repository, number, pullRequest: number, base: baseSha || baseRef, baseSha, baseRef, mergeBase, head, patchId: derivePatchId(diff), diffDigest: sha256(diff), behaviorApplicable, requirementsDigest: sha256({ repository, baseRef, baseSha }), reviewContextDigest: sha256({ repository, number, body: typeof row.body === "string" ? row.body : "" }), isDraft: row.isDraft === true, body: typeof row.body === "string" ? row.body : "", headRef: typeof row.headRefName === "string" ? row.headRefName : undefined };
  }
  openPullRequest(input: { repository: string; base: string; head: string; draft?: boolean }): PullRequestState { const args = ["pr", "create", "--repo", input.repository, "--base", input.base, "--head", input.head, ...(input.draft === false ? [] : ["--draft"])]; const url = execFileSync(this.command, args, { encoding: "utf8" }).trim(); const number = Number(url.match(/(\d+)\s*$/)?.[1] ?? 0); if (!number) throw new Error("forge did not return a pull request number"); return this.readPullRequest(input.repository, number); }
  markReady(repository: string, number: number, expectedHead: string): void { markReadyIfCurrent(this.command, repository, number, expectedHead); }
  readPolicy(repository: string): string | undefined {
    let output: string;
    try {
      output = execFileSync(this.command, ["api", `repos/${repository}/contents/${POLICY_PATH}`], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
    } catch (error) {
      if (notFound(error)) return undefined;
      throw error;
    }
    const file: unknown = JSON.parse(output);
    if (!file || typeof file !== "object" || (file as { encoding?: unknown }).encoding !== "base64" || typeof (file as { content?: unknown }).content !== "string") throw new Error(`forge returned an unreadable ${POLICY_PATH}`);
    return Buffer.from((file as { content: string }).content, "base64").toString("utf8");
  }
  readEvidence(repository: string, number: number, head: string): ForgeEvidence {
    const checkRuns = jsonLines(this.command, ["api", "--paginate", `repos/${repository}/commits/${head}/check-runs?per_page=100`, "--jq", ".check_runs[]"]).map(parseCheckRun);
    const parsed = jsonLines(this.command, ["api", "--paginate", `repos/${repository}/pulls/${number}/reviews?per_page=100`, "--jq", ".[]"]).map(parseReview);
    // REST names an App author "<slug>[bot]" as reviews do; `gh pr view --json author` would say "app/<slug>".
    const author = (jsonOutput(this.command, ["api", `repos/${repository}/pulls/${number}`]).user as { login?: unknown } | null)?.login;
    if (typeof author !== "string" || !author) throw new Error("forge returned a pull request without an author");
    const permissions = new Map<string, string>();
    for (const review of parsed) if (review.userType === "User" && review.login !== author && !permissions.has(review.login)) permissions.set(review.login, this.permissionOf(repository, review.login));
    const reviews = parsed.map((review) => ({ ...review, permission: permissions.get(review.login) ?? "none" }));
    const reviewDecision = parseReviewDecision(jsonOutput(this.command, ["pr", "view", String(number), "--repo", repository, "--json", "reviewDecision"]).reviewDecision);
    return { author, reviewDecision, checkRuns, reviews };
  }
  private permissionOf(repository: string, login: string): string {
    try {
      const output = execFileSync(this.command, ["api", `repos/${repository}/collaborators/${login}/permission`], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
      return field(JSON.parse(output) as Record<string, unknown>, "permission", isString, "collaborator permission");
    } catch (error) {
      if (notFound(error)) return "none";
      throw error;
    }
  }
}
export class OriginForge extends GhForge { constructor() { super("origin"); } }
export function markReadyIfCurrent(command: "gh" | "origin", repository: string, number: number, expectedHead: string): void {
  const args = ["pr", "view", String(number), "--json", "headRefOid,isDraft"]; if (repository) args.push("--repo", repository);
  const current = jsonOutput(command, args); if (current.headRefOid !== expectedHead) throw new Error(`head race: assessed ${expectedHead}, remote is ${String(current.headRefOid)}`); if (current.isDraft !== true) return;
  const readyArgs = ["pr", "ready", String(number)]; if (repository) readyArgs.push("--repo", repository); execFileSync(command, readyArgs, { stdio: "inherit" });
}

export class MemoryPacketStore implements PacketStore {
  readonly packets = new Map<string, ReadinessPacket>();
  get(key: string): ReadinessPacket | undefined { const packet = this.packets.get(key); return packet ? freezeRead(packet) : undefined; }
  putIfAbsent(key: string, packet: ReadinessPacket): ReadinessPacket { if (!verifyPacket(packet)) throw new Error("cannot store packet with invalid digest"); const existing = this.packets.get(key); if (existing) { if (existing.digest !== packet.digest) throw new Error(`packet digest collision for ${key}`); return freezeRead(existing); } this.packets.set(key, packet); return freezeRead(packet); }
}
/** Durable immutable packet store. Files are created with O_EXCL and never overwritten. */
export class FilePacketStore implements PacketStore {
  readonly root: string;
  constructor(root = process.env.PATINA_READINESS_STORE ?? ".patina/readiness") { this.root = resolve(root); mkdirSync(this.root, { recursive: true }); }
  private path(key: string): string { return join(this.root, `${sha256(key)}.json`); }
  get(key: string): ReadinessPacket | undefined { const path = this.path(key); if (!existsSync(path)) return undefined; const parsed: unknown = JSON.parse(readFileSync(path, "utf8")); if (!verifyPacket(parsed)) throw new Error(`immutable packet has invalid digest: ${path}`); return freezeRead(parsed); }
  putIfAbsent(key: string, packet: ReadinessPacket): ReadinessPacket {
    if (!verifyPacket(packet)) throw new Error("cannot store packet with invalid digest");
    const path = this.path(key);
    try {
      const fd = openSync(path, "wx");
      try { writeFileSync(fd, JSON.stringify(packet) + "\n"); } finally { closeSync(fd); }
      return freezeRead(packet);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
      const existing = this.get(key)!;
      if (existing.digest !== packet.digest) throw new Error(`packet digest collision for ${key}`);
      return existing;
    }
  }
}
function freezeRead(packet: ReadinessPacket): ReadinessPacket {
  const copy = JSON.parse(JSON.stringify(packet)) as ReadinessPacket;
  const freeze = <T>(value: T): T => { if (value && typeof value === "object") { Object.freeze(value); for (const child of Object.values(value as Record<string, unknown>)) freeze(child); } return value; };
  return freeze(copy);
}
export function packetKey(candidate: CandidateIdentity, bodyDigest: string): string { return `${candidate.repository}#${candidate.pullRequest}:${candidate.head}:${bodyDigest}`; }
