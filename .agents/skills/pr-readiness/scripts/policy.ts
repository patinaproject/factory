import { EVALUATOR_VERSION, OBLIGATIONS, isObligation, sha256, type Obligation, type ObligationRule, type ProducerSpec, type ReadinessPolicy } from "./domain.ts";

export const POLICY_PATH = ".github/pr-readiness.json";
const APPROVAL: ProducerSpec = { producer: "approval" };

export function parsePolicy(raw: string | undefined): ReadinessPolicy {
  const obligations = Object.fromEntries(OBLIGATIONS.map((obligation) => [obligation, APPROVAL])) as Record<Obligation, ProducerSpec>;
  if (raw !== undefined) {
    let parsed: unknown;
    try { parsed = JSON.parse(raw); } catch (error) { throw new Error(`${POLICY_PATH} is not valid JSON: ${error instanceof Error ? error.message : String(error)}`); }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error(`${POLICY_PATH} must be a JSON object`);
    const file = parsed as Record<string, unknown>;
    if (file.version !== 1) throw new Error(`${POLICY_PATH} version must be 1`);
    const declared = file.obligations;
    if (!declared || typeof declared !== "object" || Array.isArray(declared)) throw new Error(`${POLICY_PATH} obligations must be an object`);
    for (const [key, spec] of Object.entries(declared)) {
      if (!isObligation(key)) throw new Error(`${POLICY_PATH} names unknown obligation: ${key}`);
      obligations[key] = parseSpec(key, spec);
    }
  }
  return { version: 1, obligations, digest: sha256({ evaluator: EVALUATOR_VERSION, raw: raw ?? null }) };
}

function parseSpec(obligation: Obligation, value: unknown): ProducerSpec {
  const spec = (value && typeof value === "object" ? value : {}) as Record<string, unknown>;
  if (spec.producer === "approval") return APPROVAL;
  if (spec.producer !== "checks") throw new Error(`${POLICY_PATH} ${obligation} names unknown producer: ${String(spec.producer)}`);
  const checks = spec.checks;
  if (!Array.isArray(checks) || checks.length === 0 || !checks.every((name) => typeof name === "string" && name.length > 0)) {
    throw new Error(`${POLICY_PATH} ${obligation} checks must be a non-empty list of check names`);
  }
  if (spec.allowSkipped !== undefined && typeof spec.allowSkipped !== "boolean") throw new Error(`${POLICY_PATH} ${obligation} allowSkipped must be a boolean`);
  return spec.allowSkipped ? { producer: "checks", checks, allowSkipped: true } : { producer: "checks", checks };
}

export function rulesFromPolicy(policy: ReadinessPolicy): ObligationRule[] {
  return OBLIGATIONS.map((obligation) => ({
    obligation,
    spec: policy.obligations[obligation],
    applies: obligation === "behavior" ? (candidate) => candidate.behaviorApplicable !== false : () => true,
  }));
}

export interface ProduceContext { readonly repository: string; readonly pullRequest: number; readonly head: string; readonly author: string; readonly failedRunIds?: readonly number[]; }

export function produceCommand(spec: ProducerSpec, ctx: ProduceContext): string {
  if (spec.producer === "approval") {
    return `a maintainer other than @${ctx.author} reviews ${ctx.head.slice(0, 12)} and approves it: gh api repos/${ctx.repository}/pulls/${ctx.pullRequest}/reviews -f commit_id=${ctx.head} -f event=APPROVE`;
  }
  const names = spec.checks.map((name) => `"${name}"`).join(", ");
  const reruns = (ctx.failedRunIds ?? []).map((runId) => `gh run rerun ${runId} --failed --repo ${ctx.repository}`);
  return reruns.length ? `push the head so CI runs ${names}, or rerun it: ${reruns.join(" && ")}` : `push the head so CI runs ${names}`;
}
