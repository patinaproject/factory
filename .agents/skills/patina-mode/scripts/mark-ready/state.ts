import { evaluateReadiness } from "../../../pr-readiness/scripts/evaluator.ts";
import type { CandidateIdentity, Observation, ObligationRule } from "../../../pr-readiness/scripts/domain.ts";
import type { ReviewRecord } from "./types.ts";

/** Compatibility translation only. Readiness decisions belong to pr-readiness. */
export function reviewRecordObservations(record: ReviewRecord, candidate: CandidateIdentity): readonly Observation[] {
  const pass = record.status === "resolved" && record.standards.trim() !== "" && record.spec.trim() !== "" && record.blockingFindings === 0;
  return (["standards", "spec"] as const).map((obligation) => ({
    id: `legacy-${obligation}-${candidate.head}`, obligation, candidate, inputDigest: candidate.head, scope: "legacy Markdown review", artifactRefs: [record.path], producerVersion: "mark-ready-compat", capturedAt: new Date(0).toISOString(), verdict: pass ? "pass" : "fail",
    receipt: { producer: "code-review", observationId: `legacy-${obligation}-${candidate.head}`, candidateHead: candidate.head, digest: "legacy", issuedAt: new Date(0).toISOString() },
  }));
}
export function compatibilityRules(): readonly ObligationRule[] {
  return ["standards", "spec"].map((obligation) => ({ obligation: obligation as "standards" | "spec", producer: "code-review", requireReceipt: true, applies: () => true }));
}
/** Kept as a source-compatible reporting helper; it delegates to the central evaluator. */
export function checkReadiness(record: ReviewRecord, pullRequestHead: string, isDraft: boolean): { readonly ok: boolean; readonly errors: readonly string[] } {
  const candidate: CandidateIdentity = { repository: "legacy", pullRequest: 1, base: "unknown", mergeBase: "unknown", head: pullRequestHead, patchId: pullRequestHead, diffDigest: pullRequestHead, requirementsDigest: "legacy", policyDigest: "legacy" };
  const report = evaluateReadiness({ candidate, observations: reviewRecordObservations(record, candidate), rules: compatibilityRules() });
  const errors = [...(!isDraft ? ["pull request is already ready"] : []), ...(record.head !== pullRequestHead ? [`review head ${record.head} does not match current head ${pullRequestHead}`] : []), ...report.decisions.filter((d) => !["observed-current", "valid-by-equivalence", "not-applicable"].includes(d.validity)).map((d) => d.reason)];
  for (const finding of record.findings) { const resolution = record.resolutions.find((line) => line.startsWith(`${finding.id}:`)); if (!resolution) errors.push(`finding ${finding.id} has no resolution`); if (finding.kind === "hard" && resolution && /dismissed:/i.test(resolution)) errors.push(`hard finding was dismissed: ${finding.id}`); }
  return { ok: errors.length === 0, errors };
}
