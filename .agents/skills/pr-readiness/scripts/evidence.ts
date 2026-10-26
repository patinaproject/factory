import { EVALUATOR_VERSION, OBLIGATIONS, provenanceUrls, sha256, type CandidateIdentity, type CheckProvenance, type Obligation, type Observation, type Provenance, type ReadinessPolicy } from "./domain.ts";

export const ACTIONS_APP = "github-actions";
export interface CheckRunFact {
  readonly id: number;
  readonly name: string;
  readonly appSlug: string;
  readonly status: string;
  readonly conclusion: string | null;
  readonly checkSuiteId: number;
  readonly detailsUrl: string;
  readonly htmlUrl: string;
  readonly completedAt: string | null;
}
export interface ReviewFact {
  readonly id: number;
  readonly login: string;
  readonly userType: string;
  readonly permission: string;
  readonly state: string;
  readonly commitId: string;
  readonly submittedAt: string | null;
  readonly htmlUrl: string;
}
export type ReviewDecision = "APPROVED" | "CHANGES_REQUESTED" | "REVIEW_REQUIRED" | "none";
export interface ForgeEvidence { readonly author: string; readonly reviewDecision: ReviewDecision; readonly checkRuns: readonly CheckRunFact[]; readonly reviews: readonly ReviewFact[]; }
export interface Derivation { readonly observations: Observation[]; readonly notApplicable: Obligation[]; readonly notes: Partial<Record<Obligation, string>>; readonly failedRunIds: Partial<Record<Obligation, readonly number[]>>; }

const WRITERS: readonly string[] = ["admin", "write"];
const DECISIVE: readonly string[] = ["APPROVED", "CHANGES_REQUESTED", "DISMISSED"];
type Outcome = { readonly verdict: "pass" | "fail"; readonly provenance: Provenance; readonly capturedAt: string; readonly note?: string } | { readonly note: string; readonly skipped?: true };

export function deriveObservations(candidate: CandidateIdentity, policy: ReadinessPolicy, evidence: ForgeEvidence): Derivation {
  const observations: Observation[] = [];
  const notes: Partial<Record<Obligation, string>> = {};
  const failedRunIds: Partial<Record<Obligation, readonly number[]>> = {};
  const notApplicable: Obligation[] = [];
  const approval = approvalOutcome(candidate, evidence);
  for (const obligation of OBLIGATIONS) {
    const spec = policy.obligations[obligation];
    const outcome = spec.producer === "approval" ? approval : checksOutcome(spec.checks, spec.allowSkipped === true, evidence.checkRuns);
    if (outcome.note) notes[obligation] = outcome.note;
    if (!("verdict" in outcome)) { if (outcome.skipped) notApplicable.push(obligation); continue; }
    observations.push(observation(candidate, obligation, outcome));
    if (outcome.verdict === "fail" && outcome.provenance.kind === "checks") {
      failedRunIds[obligation] = [...new Set(outcome.provenance.checks.filter((check) => check.conclusion !== "success" && check.runId !== undefined).map((check) => check.runId!))];
    }
  }
  return { observations, notApplicable, notes, failedRunIds };
}

function checksOutcome(names: readonly string[], allowSkipped: boolean, runs: readonly CheckRunFact[]): Outcome {
  const kept = new Map<string, CheckRunFact[]>();
  for (const name of names) {
    const newestPerSuite = new Map<number, CheckRunFact>();
    for (const run of runs) {
      if (run.appSlug !== ACTIONS_APP || run.name !== name) continue;
      const current = newestPerSuite.get(run.checkSuiteId);
      if (!current || run.id > current.id) newestPerSuite.set(run.checkSuiteId, run);
    }
    kept.set(name, [...newestPerSuite.values()]);
  }
  const missing = names.filter((name) => kept.get(name)!.length === 0);
  const pending = names.filter((name) => kept.get(name)!.some((run) => run.status !== "completed"));
  if (missing.length || pending.length) {
    return { note: [missing.length ? `no GitHub Actions run of ${quote(missing)}` : "", pending.length ? `${quote(pending)} still running` : ""].filter(Boolean).join("; ") };
  }
  const all = names.flatMap((name) => kept.get(name)!);
  if (allowSkipped && all.every((run) => run.conclusion === "skipped")) return { note: `${quote(names)} skipped`, skipped: true };
  const failing = all.filter((run) => run.conclusion !== "success");
  const checks: CheckProvenance[] = all.map((run) => ({ name: run.name, checkRunId: run.id, runId: runIdFrom(run.detailsUrl), conclusion: run.conclusion ?? "none", url: run.htmlUrl }));
  const capturedAt = all.map((run) => run.completedAt!).sort().at(-1)!;
  return {
    verdict: failing.length ? "fail" : "pass",
    provenance: { kind: "checks", checks },
    capturedAt,
    note: failing.length ? `failing: ${failing.map((run) => `"${run.name}" (${run.conclusion ?? "none"})`).join(", ")}` : undefined,
  };
}

function approvalOutcome(candidate: CandidateIdentity, evidence: ForgeEvidence): Outcome {
  const ignored: string[] = [];
  const latest = new Map<string, ReviewFact>();
  for (const review of evidence.reviews) {
    if (!DECISIVE.includes(review.state)) continue;
    const reason = review.login === evidence.author ? `review by PR author @${review.login} ignored`
      : review.userType !== "User" ? `review by ${review.userType} @${review.login} ignored`
      : !WRITERS.includes(review.permission) ? `review by @${review.login} without write access (${review.permission}) ignored`
      : undefined;
    if (reason) { ignored.push(reason); continue; }
    const current = latest.get(review.login);
    if (!current || review.id > current.id) latest.set(review.login, review);
  }
  const standing = [...latest.values()].sort((a, b) => a.id - b.id);
  const blocking = standing.find((review) => review.state === "CHANGES_REQUESTED");
  if (blocking) return { verdict: "fail", provenance: approvalProvenance(blocking), capturedAt: blocking.submittedAt!, note: `@${blocking.login} requested changes` };
  const approvals = standing.filter((review) => review.state === "APPROVED");
  for (const stale of approvals) if (stale.commitId !== candidate.head) ignored.push(`review by @${stale.login} on older commit ${stale.commitId.slice(0, 12)} ignored`);
  const approved = approvals.find((review) => review.commitId === candidate.head);
  if (!approved) return { note: [`no maintainer approval at ${candidate.head.slice(0, 12)}`, ...ignored].join("; ") };
  if (evidence.reviewDecision !== "APPROVED") {
    return { note: evidence.reviewDecision === "none" ? "GitHub computes no review decision, so the ruleset requires no approving review" : `GitHub review decision is ${evidence.reviewDecision}, not APPROVED` };
  }
  return { verdict: "pass", provenance: approvalProvenance(approved), capturedAt: approved.submittedAt! };
}

function approvalProvenance(review: ReviewFact): Provenance { return { kind: "approval", reviewer: review.login, reviewId: review.id, url: review.htmlUrl }; }

function observation(candidate: CandidateIdentity, obligation: Obligation, outcome: Extract<Outcome, { verdict: unknown }>): Observation {
  const { provenance } = outcome;
  const scope = provenance.kind === "checks" ? provenance.checks.map((check) => check.name).join(", ") : `review ${provenance.reviewId}`;
  return {
    id: `${obligation}@${candidate.head}`,
    obligation,
    candidate,
    inputDigest: sha256(provenance),
    scope,
    artifactRefs: provenanceUrls(provenance),
    producerVersion: EVALUATOR_VERSION,
    capturedAt: outcome.capturedAt,
    verdict: outcome.verdict,
    provenance,
  };
}

function runIdFrom(detailsUrl: string): number | undefined {
  const match = detailsUrl.match(/\/actions\/runs\/(\d+)\//);
  return match ? Number(match[1]) : undefined;
}
function quote(names: readonly string[]): string { return names.map((name) => `"${name}"`).join(", "); }
