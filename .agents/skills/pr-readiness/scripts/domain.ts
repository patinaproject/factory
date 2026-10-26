import { createHash } from "node:crypto";

export type Validity = "observed-current" | "valid-by-equivalence" | "rerun-required" | "invalid" | "not-applicable";
export type Obligation = "standards" | "spec" | "hygiene" | "tests" | "lint" | "behavior";
export const OBLIGATIONS: readonly Obligation[] = ["standards", "spec", "hygiene", "tests", "lint", "behavior"];

/** Every field is derived from the forge and git, never from PR-head code. */
export interface CandidateIdentity {
  readonly repository: string;
  readonly pullRequest: number;
  /** `base` is retained as a wire-format alias for older producers. */
  readonly base: string;
  readonly baseSha?: string;
  readonly baseRef?: string;
  readonly mergeBase: string;
  readonly head: string;
  readonly patchId: string;
  readonly diffDigest: string;
  readonly behaviorApplicable?: boolean;
  readonly requirementsDigest: string;
  readonly policyDigest: string;
  readonly reviewContextDigest?: string;
}
export interface CheckProvenance { readonly name: string; readonly checkRunId: number; readonly runId?: number; readonly conclusion: string; readonly url: string; }
export type Provenance =
  | { readonly kind: "checks"; readonly checks: readonly CheckProvenance[] }
  | { readonly kind: "approval"; readonly reviewer: string; readonly reviewId: number; readonly url: string };
export const EVALUATOR_VERSION = "pr-readiness/4";
export function provenanceUrls(provenance: Provenance): string[] { return provenance.kind === "approval" ? [provenance.url] : provenance.checks.map((check) => check.url); }
export type ProducerSpec = { readonly producer: "checks"; readonly checks: readonly string[]; readonly allowSkipped?: boolean } | { readonly producer: "approval" };
export interface ReadinessPolicy { readonly version: 1; readonly obligations: Readonly<Record<Obligation, ProducerSpec>>; readonly digest: string; }
export interface Observation {
  readonly id: string;
  readonly obligation: Obligation;
  readonly candidate: CandidateIdentity;
  readonly inputDigest: string;
  readonly scope: string;
  readonly artifactRefs: readonly string[];
  readonly producerVersion: string;
  readonly capturedAt: string;
  readonly expiresAt?: string;
  readonly verdict: "pass" | "fail";
  readonly reviewContextDigest?: string;
  readonly provenance: Provenance;
}
export interface ValidityDecision { readonly obligation: Obligation; readonly observationId?: string; readonly validity: Validity; readonly reason: string; readonly capturedHead?: string; readonly produce?: string; }
export interface ObligationRule { readonly obligation: Obligation; readonly applies: (candidate: CandidateIdentity) => boolean; readonly spec: ProducerSpec; }
export interface ReadinessPacket {
  readonly version: 1;
  readonly candidate: CandidateIdentity;
  readonly observationIds: readonly string[];
  readonly decisions: readonly ValidityDecision[];
  readonly requirementsDigest: string;
  readonly policyDigest: string;
  readonly bodyDigest: string;
  readonly provenanceRefs: readonly string[];
  readonly artifactRefs: readonly string[];
  readonly expiresAt?: string;
  readonly generatedAt?: string;
  readonly generation?: number;
  readonly digest: string;
}
export function canonicalize(value: unknown): string {
  if (value === undefined) return "null";
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalize).join(",")}]`;
  const object = value as Record<string, unknown>;
  return `{${Object.keys(object).sort().map((key) => `${JSON.stringify(key)}:${canonicalize(object[key])}`).join(",")}}`;
}
export function sha256(value: unknown): string { return createHash("sha256").update(canonicalize(value)).digest("hex"); }
export function isObligation(value: unknown): value is Obligation { return typeof value === "string" && (OBLIGATIONS as readonly string[]).includes(value); }
export function freezePacket(packet: Omit<ReadinessPacket, "digest">): ReadinessPacket { return deepFreeze({ ...packet, digest: sha256(packet) } as ReadinessPacket); }
export function verifyPacket(packet: unknown): packet is ReadinessPacket {
  if (!packet || typeof packet !== "object") return false;
  const value = packet as Partial<ReadinessPacket>;
  if (value.version !== 1 || typeof value.digest !== "string" || !value.candidate || !Array.isArray(value.decisions)) return false;
  const { digest, ...unsigned } = value as ReadinessPacket;
  return sha256(unsigned) === digest;
}
function deepFreeze<T>(value: T): T { if (value && typeof value === "object") { Object.freeze(value); for (const child of Object.values(value as Record<string, unknown>)) deepFreeze(child); } return value; }
