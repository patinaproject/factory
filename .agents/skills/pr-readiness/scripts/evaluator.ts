import {
  type BodyClaim, type CandidateIdentity, type ExecutionContext, type Obligation, type ObligationRule,
  type Observation, type ReadinessPacket, type ValidityDecision, freezePacket, isObligation, sha256,
} from "./domain.ts";

export interface EvaluationInput {
  readonly candidate: CandidateIdentity;
  readonly observations: readonly Observation[];
  readonly bodyClaims?: readonly BodyClaim[];
  readonly now?: string;
  readonly body?: string;
  readonly rules?: readonly ObligationRule[];
  /** Expected producer inputs are supplied by the trusted evaluator, never by PR code. */
  readonly expectedExecution?: Readonly<Partial<Record<Obligation, ExecutionContext>>>;
  /** Legacy spelling accepted for callers migrating to expectedExecution. */
  readonly executionInputs?: Readonly<Partial<Record<Obligation, string | ExecutionContext>>>;
}
export interface EvaluationReport { readonly ready: boolean; readonly decisions: readonly ValidityDecision[]; readonly bodyErrors: readonly string[]; readonly packet?: ReadinessPacket; }

export const DEFAULT_RULES: readonly ObligationRule[] = [
  { obligation: "standards", producer: "code-review", requireReceipt: true, applies: () => true },
  { obligation: "spec", producer: "code-review", requireReceipt: true, applies: () => true },
  { obligation: "hygiene", producer: "deslop/no-comments", requireReceipt: true, applies: () => true },
  { obligation: "tests", producer: "test", requireReceipt: true, applies: () => true },
  { obligation: "lint", producer: "lint", requireReceipt: true, applies: () => true },
  { obligation: "behavior", producer: "runtime-verification", requireReceipt: true, applies: (candidate) => candidate.behaviorApplicable !== false },
];
const ACCEPTED: readonly string[] = ["observed-current", "valid-by-equivalence", "not-applicable"];

function validCandidate(value: unknown): value is CandidateIdentity {
  if (!value || typeof value !== "object") return false;
  const c = value as Partial<CandidateIdentity>;
  return typeof c.repository === "string" && Number.isInteger(c.pullRequest) && (c.pullRequest ?? 0) > 0 &&
    typeof c.base === "string" && typeof c.mergeBase === "string" && typeof c.head === "string" &&
    typeof c.patchId === "string" && typeof c.diffDigest === "string" && typeof c.requirementsDigest === "string" && typeof c.policyDigest === "string" &&
    (c.behaviorApplicable === undefined || typeof c.behaviorApplicable === "boolean") &&
    (c.baseSha === undefined || typeof c.baseSha === "string") && (c.baseRef === undefined || typeof c.baseRef === "string") &&
    (c.reviewContextDigest === undefined || typeof c.reviewContextDigest === "string");
}
function validObservation(value: unknown): value is Observation {
  if (!value || typeof value !== "object") return false;
  const o = value as Partial<Observation>;
  return typeof o.id === "string" && o.id.length > 0 && isObligation(o.obligation) && validCandidate(o.candidate) &&
    typeof o.inputDigest === "string" && typeof o.scope === "string" && Array.isArray(o.artifactRefs) && o.artifactRefs.every((r) => typeof r === "string") &&
    typeof o.producerVersion === "string" && Number.isFinite(Date.parse(o.capturedAt ?? "")) &&
    (o.expiresAt === undefined || Number.isFinite(Date.parse(o.expiresAt))) && (o.verdict === "pass" || o.verdict === "fail") &&
    (!o.receipt || (typeof o.receipt === "object" && o.receipt.observationId === o.id && typeof o.receipt.producer === "string" && o.receipt.candidateHead === o.candidate.head && typeof o.receipt.digest === "string"));
}
function equalCandidate(a: CandidateIdentity, b: CandidateIdentity): boolean {
  return ["repository", "pullRequest", "base", "baseSha", "baseRef", "mergeBase", "head", "patchId", "diffDigest", "behaviorApplicable", "requirementsDigest", "policyDigest", "reviewContextDigest"]
    .every((k) => (a as unknown as Record<string, unknown>)[k] === (b as unknown as Record<string, unknown>)[k]);
}
function expectedFor(input: EvaluationInput, obligation: Obligation): ExecutionContext | undefined {
  const value = input.expectedExecution?.[obligation] ?? input.executionInputs?.[obligation];
  return typeof value === "string" ? { inputDigest: value } : value;
}
function contextMatches(observation: Observation, expected: ExecutionContext | undefined): boolean {
  if (!expected) return true;
  return Object.entries(expected).every(([key, value]) => value === undefined || (observation as unknown as Record<string, unknown>)[key] === value);
}
function artifactIdentity(o: Observation): string {
  return o.artifactDigest ?? (o.artifactRefs.length ? sha256(o.artifactRefs) : "");
}
function completeBehaviorContext(o: Observation): boolean {
  return [o.executableArtifact, o.target, o.runtime, o.environment, o.fixtures, o.scope, o.freshness].every((value) => typeof value === "string" && value.length > 0);
}
function equivalent(obligation: Obligation, current: CandidateIdentity, captured: CandidateIdentity, o: Observation, expected?: ExecutionContext): boolean {
  if (obligation === "standards" || obligation === "spec") {
    return current.patchId === captured.patchId && current.requirementsDigest === captured.requirementsDigest && current.policyDigest === captured.policyDigest &&
      (current.reviewContextDigest ?? o.reviewContextDigest) === (captured.reviewContextDigest ?? o.reviewContextDigest) && contextMatches(o, expected);
  }
  if (obligation === "hygiene") return current.diffDigest === captured.diffDigest && current.policyDigest === captured.policyDigest && contextMatches(o, expected);
  if (obligation === "tests" || obligation === "lint") {
    return current.diffDigest === captured.diffDigest && current.baseSha === captured.baseSha && current.mergeBase === captured.mergeBase && o.inputDigest === (expected?.inputDigest ?? o.inputDigest) &&
      artifactIdentity(o) === (expected?.artifactDigest ?? artifactIdentity(o)) && contextMatches(o, expected);
  }
  // Runtime evidence may be reused only when all executable/runtime dimensions match.
  if (obligation === "behavior") {
    const keys: readonly (keyof ExecutionContext)[] = ["executableArtifact", "target", "runtime", "environment", "fixtures", "scope", "freshness"];
    return keys.every((key) => {
      const expectedValue = expected?.[key];
      const capturedValue = o[key];
      return expectedValue !== undefined && capturedValue === expectedValue;
    }) && (expected?.artifactDigest === undefined || artifactIdentity(o) === expected.artifactDigest) &&
      (expected?.inputDigest === undefined || o.inputDigest === expected.inputDigest);
  }
  return false;
}

export function evaluateReadiness(input: EvaluationInput): EvaluationReport {
  const rules = input.rules ?? DEFAULT_RULES;
  const now = Date.parse(input.now ?? new Date().toISOString());
  if (!Number.isFinite(now)) return { ready: false, decisions: [], bodyErrors: ["invalid evaluator time"] };
  const bodyErrors: string[] = [];
  if (!validCandidate(input.candidate)) bodyErrors.push("malformed candidate identity");
  const seen = new Set<string>();
  const observations = input.observations ?? [];
  for (const observation of observations) {
    if (!validObservation(observation)) { bodyErrors.push("malformed observation"); continue; }
    if (seen.has(observation.id)) bodyErrors.push(`duplicate observation ID: ${observation.id}`);
    seen.add(observation.id);
  }
  const ruleObligations = new Set<Obligation>();
  for (const rule of rules) {
    if (ruleObligations.has(rule.obligation)) bodyErrors.push(`duplicate obligation rule: ${rule.obligation}`);
    ruleObligations.add(rule.obligation);
  }
  for (const observation of observations) if (validObservation(observation) && !ruleObligations.has(observation.obligation)) bodyErrors.push(`unknown obligation: ${observation.obligation}`);
  const decisions: ValidityDecision[] = [];
  for (const rule of rules) {
    if (!rule.applies(input.candidate)) { decisions.push({ obligation: rule.obligation, validity: "not-applicable", reason: `change does not require ${rule.obligation} evidence` }); continue; }
    const candidates = observations.filter((o) => validObservation(o) && o.obligation === rule.obligation);
    if (candidates.length === 0) { decisions.push({ obligation: rule.obligation, validity: "rerun-required", reason: `missing ${rule.obligation} observation` }); continue; }
    if (candidates.length > 1) { decisions.push({ obligation: rule.obligation, validity: "invalid", reason: `duplicate ${rule.obligation} observations` }); continue; }
    const observation = candidates[0]!;
    if (observation.candidate.repository !== input.candidate.repository || observation.candidate.pullRequest !== input.candidate.pullRequest) { decisions.push({ obligation: rule.obligation, observationId: observation.id, validity: "invalid", reason: `${rule.obligation} observation belongs to another pull request`, capturedHead: observation.candidate.head }); continue; }
    if (rule.requireReceipt && (!observation.receipt || observation.receipt.producer !== rule.producer || observation.receipt.observationId !== observation.id)) { decisions.push({ obligation: rule.obligation, observationId: observation.id, validity: "invalid", reason: `${rule.obligation} observation has no producer-bound receipt`, capturedHead: observation.candidate.head }); continue; }
    if (observation.verdict !== "pass") { decisions.push({ obligation: rule.obligation, observationId: observation.id, validity: "invalid", reason: `${rule.obligation} observation failed`, capturedHead: observation.candidate.head }); continue; }
    if (observation.expiresAt && Date.parse(observation.expiresAt) <= now) { decisions.push({ obligation: rule.obligation, observationId: observation.id, validity: "rerun-required", reason: `${rule.obligation} observation expired`, capturedHead: observation.candidate.head }); continue; }
    const expected = expectedFor(input, rule.obligation);
    if (!contextMatches(observation, expected)) { decisions.push({ obligation: rule.obligation, observationId: observation.id, validity: "rerun-required", reason: `${rule.obligation} execution inputs changed`, capturedHead: observation.candidate.head }); continue; }
    if (equalCandidate(input.candidate, observation.candidate) && (rule.obligation !== "behavior" || completeBehaviorContext(observation))) { decisions.push({ obligation: rule.obligation, observationId: observation.id, validity: "observed-current", reason: "observation matches current candidate", capturedHead: observation.candidate.head }); continue; }
    if (equivalent(rule.obligation, input.candidate, observation.candidate, observation, expected)) decisions.push({ obligation: rule.obligation, observationId: observation.id, validity: "valid-by-equivalence", reason: "producer equivalence contract permits reuse", capturedHead: observation.candidate.head });
    else decisions.push({ obligation: rule.obligation, observationId: observation.id, validity: "rerun-required", reason: `${rule.obligation} observation does not describe current candidate`, capturedHead: observation.candidate.head });
  }
  bodyErrors.push(...validateBodyClaims(input.body, input.bodyClaims, decisions));
  const ready = bodyErrors.length === 0 && decisions.every((d) => ACCEPTED.includes(d.validity));
  if (!ready) return { ready, decisions, bodyErrors };
  const packet = freezePacket({ version: 1, candidate: input.candidate, observationIds: decisions.flatMap((d) => d.observationId ? [d.observationId] : []), decisions, requirementsDigest: input.candidate.requirementsDigest, policyDigest: input.candidate.policyDigest, bodyDigest: sha256(input.body ?? ""), receiptRefs: decisions.flatMap((d) => d.observationId ? [`receipt:${d.observationId}`] : []), artifactRefs: observations.flatMap((o) => o.artifactRefs), generatedAt: new Date(now).toISOString() });
  return { ready, decisions, bodyErrors, packet };
}

function validateBodyClaims(body: string | undefined, claims: readonly BodyClaim[] | undefined, decisions: readonly ValidityDecision[]): string[] {
  if (body === undefined && claims === undefined) return [];
  const errors: string[] = [];
  const evidenceStart = body?.search(/^## Evidence\s*$/im) ?? -1;
  const evidenceEnd = evidenceStart < 0 ? -1 : body!.slice(evidenceStart + 1).search(/^##\s+/m);
  if (body !== undefined && decisions.some((decision) => decision.observationId) && evidenceStart < 0) errors.push("body is missing an Evidence section");
  const byId = new Map(decisions.filter((d) => d.observationId).map((d) => [d.observationId!, d]));
  const seen = new Set<string>();
  for (const claim of claims ?? []) {
    if (!claim || typeof claim.observationId !== "string" || !isObligation(claim.obligation) || typeof claim.validity !== "string") { errors.push("malformed body claim"); continue; }
    if (seen.has(claim.observationId)) errors.push(`duplicate observation ID: ${claim.observationId}`);
    seen.add(claim.observationId);
    const decision = byId.get(claim.observationId);
    if (!decision) errors.push(`unknown observation ID: ${claim.observationId}`);
    else if (decision.obligation !== claim.obligation || decision.validity !== claim.validity) errors.push(`body claim disagrees for ${claim.observationId}`);
    if (body !== undefined && (!body.includes(claim.observationId) || evidenceStart < 0 || (evidenceEnd >= 0 && body.indexOf(claim.observationId) > evidenceStart + evidenceEnd + 1))) errors.push(`body omits observation ID from Evidence section: ${claim.observationId}`);
  }
  for (const decision of decisions) if (decision.observationId && !seen.has(decision.observationId)) errors.push(`body omits observation ID: ${decision.observationId}`);
  return errors;
}
export function parseBodyClaims(body: string): readonly BodyClaim[] {
  const claims: BodyClaim[] = [];
  for (const match of body.matchAll(/<!--\s*pr-readiness:\s*(\{[^\n]+\})\s*-->/g)) {
    let parsed: unknown;
    try { parsed = JSON.parse(match[1]!); } catch { throw new Error("malformed readiness body claim"); }
    if (!parsed || typeof parsed !== "object") throw new Error("malformed readiness body claim");
    const claim = parsed as Partial<BodyClaim>;
    if (typeof claim.observationId !== "string" || !isObligation(claim.obligation) || typeof claim.validity !== "string") throw new Error("malformed readiness body claim");
    claims.push(claim as BodyClaim);
  }
  return claims;
}
