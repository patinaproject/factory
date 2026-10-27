import {
  type CandidateIdentity, type Obligation, type ObligationRule,
  type Observation, type Provenance, type CheckProvenance, type ReadinessPacket, type Validity, type ValidityDecision, freezePacket, isObligation, provenanceUrls, sha256,
} from "./domain.ts";

export interface EvaluationInput {
  readonly candidate: CandidateIdentity;
  readonly observations: readonly Observation[];
  readonly now?: string;
  readonly body?: string;
  readonly rules: readonly ObligationRule[];
}
export interface EvaluationReport { readonly ready: boolean; readonly decisions: readonly ValidityDecision[]; readonly errors: readonly string[]; readonly packet?: ReadinessPacket; }

export const ACCEPTED: readonly Validity[] = ["observed-current", "valid-by-equivalence", "not-applicable"];

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
    validProvenance(o.provenance);
}
function validProvenance(value: unknown): value is Provenance {
  if (!value || typeof value !== "object") return false;
  const p = value as Partial<Record<string, unknown>>;
  if (p.kind === "approval") return typeof p.reviewer === "string" && Number.isInteger(p.reviewId) && typeof p.url === "string";
  return p.kind === "checks" && Array.isArray(p.checks) && p.checks.length > 0 && p.checks.every((c: Partial<CheckProvenance>) =>
    !!c && typeof c.name === "string" && Number.isInteger(c.checkRunId) && (c.runId === undefined || Number.isInteger(c.runId)) && typeof c.conclusion === "string" && typeof c.url === "string");
}
function equalCandidate(a: CandidateIdentity, b: CandidateIdentity): boolean {
  return ["repository", "pullRequest", "base", "baseSha", "baseRef", "mergeBase", "head", "patchId", "diffDigest", "behaviorApplicable", "requirementsDigest", "policyDigest", "reviewContextDigest"]
    .every((k) => (a as unknown as Record<string, unknown>)[k] === (b as unknown as Record<string, unknown>)[k]);
}
function equivalent(obligation: Obligation, current: CandidateIdentity, captured: CandidateIdentity, o: Observation): boolean {
  if (obligation === "standards" || obligation === "spec") {
    return current.patchId === captured.patchId && current.requirementsDigest === captured.requirementsDigest && current.policyDigest === captured.policyDigest &&
      (current.reviewContextDigest ?? o.reviewContextDigest) === (captured.reviewContextDigest ?? o.reviewContextDigest);
  }
  if (obligation === "hygiene") return current.diffDigest === captured.diffDigest && current.policyDigest === captured.policyDigest;
  if (obligation === "tests" || obligation === "lint") {
    return current.diffDigest === captured.diffDigest && current.baseSha === captured.baseSha && current.mergeBase === captured.mergeBase;
  }
  return false;
}

export function evaluateReadiness(input: EvaluationInput): EvaluationReport {
  const rules = input.rules;
  const now = Date.parse(input.now ?? new Date().toISOString());
  if (!Number.isFinite(now)) return { ready: false, decisions: [], errors: ["invalid evaluator time"] };
  const errors: string[] = [];
  if (!validCandidate(input.candidate)) errors.push("malformed candidate identity");
  const seen = new Set<string>();
  const observations = input.observations ?? [];
  for (const observation of observations) {
    if (!validObservation(observation)) { errors.push("malformed observation"); continue; }
    if (seen.has(observation.id)) errors.push(`duplicate observation ID: ${observation.id}`);
    seen.add(observation.id);
  }
  const ruleObligations = new Set<Obligation>();
  for (const rule of rules) {
    if (ruleObligations.has(rule.obligation)) errors.push(`duplicate obligation rule: ${rule.obligation}`);
    ruleObligations.add(rule.obligation);
  }
  for (const observation of observations) if (validObservation(observation) && !ruleObligations.has(observation.obligation)) errors.push(`unknown obligation: ${observation.obligation}`);
  const decisions: ValidityDecision[] = [];
  for (const rule of rules) {
    if (!rule.applies(input.candidate)) { decisions.push({ obligation: rule.obligation, validity: "not-applicable", reason: `change does not require ${rule.obligation} evidence` }); continue; }
    const candidates = observations.filter((o) => validObservation(o) && o.obligation === rule.obligation);
    if (candidates.length === 0) { decisions.push({ obligation: rule.obligation, validity: "rerun-required", reason: `missing ${rule.obligation} observation` }); continue; }
    if (candidates.length > 1) { decisions.push({ obligation: rule.obligation, validity: "invalid", reason: `duplicate ${rule.obligation} observations` }); continue; }
    const observation = candidates[0]!;
    if (observation.candidate.repository !== input.candidate.repository || observation.candidate.pullRequest !== input.candidate.pullRequest) { decisions.push({ obligation: rule.obligation, observationId: observation.id, validity: "invalid", reason: `${rule.obligation} observation belongs to another pull request`, capturedHead: observation.candidate.head }); continue; }
    if (observation.provenance.kind !== rule.spec.producer) { decisions.push({ obligation: rule.obligation, observationId: observation.id, validity: "invalid", reason: `${rule.obligation} observation was not produced by ${rule.spec.producer}`, capturedHead: observation.candidate.head }); continue; }
    if (observation.verdict !== "pass") { decisions.push({ obligation: rule.obligation, observationId: observation.id, validity: "invalid", reason: `${rule.obligation} observation failed`, capturedHead: observation.candidate.head }); continue; }
    if (observation.expiresAt && Date.parse(observation.expiresAt) <= now) { decisions.push({ obligation: rule.obligation, observationId: observation.id, validity: "rerun-required", reason: `${rule.obligation} observation expired`, capturedHead: observation.candidate.head }); continue; }
    if (equalCandidate(input.candidate, observation.candidate)) { decisions.push({ obligation: rule.obligation, observationId: observation.id, validity: "observed-current", reason: "observation matches current candidate", capturedHead: observation.candidate.head }); continue; }
    if (equivalent(rule.obligation, input.candidate, observation.candidate, observation)) decisions.push({ obligation: rule.obligation, observationId: observation.id, validity: "valid-by-equivalence", reason: "producer equivalence contract permits reuse", capturedHead: observation.candidate.head });
    else decisions.push({ obligation: rule.obligation, observationId: observation.id, validity: "rerun-required", reason: `${rule.obligation} observation does not describe current candidate`, capturedHead: observation.candidate.head });
  }
  const ready = errors.length === 0 && decisions.every((d) => ACCEPTED.includes(d.validity));
  if (!ready) return { ready, decisions, errors };
  const packet = freezePacket({ version: 1, candidate: input.candidate, observationIds: decisions.flatMap((d) => d.observationId ? [d.observationId] : []), decisions, requirementsDigest: input.candidate.requirementsDigest, policyDigest: input.candidate.policyDigest, bodyDigest: sha256(input.body ?? ""), provenanceRefs: observations.flatMap((o) => provenanceUrls(o.provenance)), artifactRefs: observations.flatMap((o) => o.artifactRefs), generatedAt: new Date(now).toISOString() });
  return { ready, decisions, errors, packet };
}
