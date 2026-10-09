import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, openSync, closeSync, writeFileSync, existsSync } from "node:fs";
import { join, resolve } from "node:path";
import { sha256, verifyPacket, type CandidateIdentity, type ReadinessPacket } from "./domain.ts";

export interface PullRequestState extends CandidateIdentity { readonly number: number; readonly isDraft: boolean; readonly body?: string; readonly baseRef?: string; readonly headRef?: string; }
export interface Forge {
  readPullRequest(repository: string, number: number): PullRequestState;
  openPullRequest(input: { repository: string; base: string; head: string; draft?: boolean }): PullRequestState;
  markReady(repository: string, number: number, expectedHead: string): void;
  publishStatus?(repository: string, head: string, state: "success" | "failure", description: string): void;
}
export interface PacketStore { get(key: string): ReadinessPacket | undefined; putIfAbsent(key: string, packet: ReadinessPacket): ReadinessPacket; }

export function candidateFromPullRequest(pr: PullRequestState): CandidateIdentity {
  return { repository: pr.repository, pullRequest: pr.pullRequest || pr.number, base: pr.base, baseSha: pr.baseSha, baseRef: pr.baseRef, mergeBase: pr.mergeBase, head: pr.head, patchId: pr.patchId, diffDigest: pr.diffDigest, behaviorApplicable: pr.behaviorApplicable, requirementsDigest: pr.requirementsDigest, policyDigest: pr.policyDigest, reviewContextDigest: pr.reviewContextDigest };
}

function jsonOutput(command: string, args: readonly string[]): Record<string, unknown> { const output = execFileSync(command, [...args], { encoding: "utf8" }); const parsed: unknown = JSON.parse(output); if (!parsed || typeof parsed !== "object") throw new Error("forge returned invalid JSON"); return parsed as Record<string, unknown>; }
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
    return { repository, number, pullRequest: number, base: baseSha || baseRef, baseSha, baseRef, mergeBase, head, patchId: derivePatchId(diff), diffDigest: sha256(diff), behaviorApplicable, requirementsDigest: sha256({ repository, baseRef, baseSha }), policyDigest: sha256({ workflow: "pr-readiness", policy: "v2" }), reviewContextDigest: sha256({ repository, number, body: typeof row.body === "string" ? row.body : "" }), isDraft: row.isDraft === true, body: typeof row.body === "string" ? row.body : "", headRef: typeof row.headRefName === "string" ? row.headRefName : undefined };
  }
  openPullRequest(input: { repository: string; base: string; head: string; draft?: boolean }): PullRequestState { const args = ["pr", "create", "--repo", input.repository, "--base", input.base, "--head", input.head, ...(input.draft === false ? [] : ["--draft"])]; const url = execFileSync(this.command, args, { encoding: "utf8" }).trim(); const number = Number(url.match(/(\d+)\s*$/)?.[1] ?? 0); if (!number) throw new Error("forge did not return a pull request number"); return this.readPullRequest(input.repository, number); }
  markReady(repository: string, number: number, expectedHead: string): void { markReadyIfCurrent(this.command, repository, number, expectedHead); }
  publishStatus(repository: string, head: string, state: "success" | "failure", description: string): void { execFileSync(this.command, ["api", `repos/${repository}/statuses/${head}`, "-f", `state=${state}`, "-f", "context=PR readiness", "-f", `description=${description.slice(0, 140)}`], { stdio: "inherit" }); }
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
