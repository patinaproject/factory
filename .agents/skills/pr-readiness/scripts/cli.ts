import type { CandidateIdentity, ReadinessPolicy, ValidityDecision } from "./domain.ts";
import { ACCEPTED, evaluateReadiness, type EvaluationReport } from "./evaluator.ts";
import { deriveObservations } from "./evidence.ts";
import { parsePolicy, produceCommand, rulesFromPolicy } from "./policy.ts";
import { GhForge, FilePacketStore, candidateFromPullRequest, packetKey, type Forge, type PacketStore, type PullRequestState } from "./adapters.ts";

export interface ReadinessDependencies { readonly forge: Forge; readonly store: PacketStore; }
interface Assessment { readonly pr: PullRequestState; readonly candidate: CandidateIdentity; readonly policy: ReadinessPolicy; readonly report: EvaluationReport; }

function value(argv: readonly string[], name: string): string | undefined { const i = argv.indexOf(name); return i >= 0 && i + 1 < argv.length ? argv[i + 1] : undefined; }
function required(argv: readonly string[], name: string): string { const result = value(argv, name); if (!result || result.startsWith("--")) throw new Error(`${name} is required`); return result; }
const unmet = (report: EvaluationReport): readonly ValidityDecision[] => report.decisions.filter((d) => !ACCEPTED.includes(d.validity));
function reportError(report: EvaluationReport): string { return [...unmet(report).map((d) => d.reason), ...report.errors].join("; ") || "readiness failed"; }

function assess(forge: Forge, repository: string, number: number, bodyOverride: string | undefined, now: string | undefined): Assessment {
  const pr = forge.readPullRequest(repository, number);
  const policy = parsePolicy(forge.readPolicy(repository));
  const candidate = candidateFromPullRequest(pr, policy.digest);
  const evidence = forge.readEvidence(repository, number, candidate.head);
  const { observations, notApplicable, notes, failedRunIds } = deriveObservations(candidate, policy, evidence);
  const rules = rulesFromPolicy(policy).map((rule) => notApplicable.includes(rule.obligation) ? { ...rule, applies: () => false } : rule);
  const evaluated = evaluateReadiness({ candidate, observations, body: bodyOverride ?? pr.body, now, rules });
  const decisions = evaluated.decisions.map((decision): ValidityDecision => {
    if (ACCEPTED.includes(decision.validity)) return decision;
    const spec = policy.obligations[decision.obligation];
    const note = notes[decision.obligation];
    return { ...decision, reason: note ? `${decision.reason}: ${note}` : decision.reason, produce: produceCommand(spec, { repository, pullRequest: number, head: candidate.head, author: evidence.author, failedRunIds: failedRunIds[decision.obligation] }) };
  });
  return { pr, candidate, policy, report: { ...evaluated, decisions } };
}

export function operation(argv: readonly string[], deps: ReadinessDependencies): number {
  try {
    const command = argv[0]; if (!command || !["open", "check", "publish"].includes(command)) throw new Error("usage: pr-readiness open|check|publish");
    const repository = required(argv, "--repo");
    if (command === "open") { const mode = value(argv, "--mode"); if (mode !== undefined && mode !== "draft" && mode !== "ready") throw new Error("--mode must be draft or ready"); const draft = mode ? mode === "draft" : value(argv, "--draft") !== "false"; const state = deps.forge.openPullRequest({ repository, base: required(argv, "--base"), head: required(argv, "--head"), draft }); process.stdout.write(JSON.stringify(state) + "\n"); return 0; }
    const number = Number(required(argv, "--pr")); if (!Number.isInteger(number) || number < 1) throw new Error("--pr requires a positive integer");
    const now = value(argv, "--now");
    const initial = assess(deps.forge, repository, number, value(argv, "--body"), now);
    const { report } = initial;
    if (command === "check") {
      process.stdout.write(JSON.stringify(report) + "\n");
      for (const d of unmet(report)) process.stderr.write(`${d.obligation}: ${d.reason} -> ${d.produce}\n`);
      for (const error of report.errors) process.stderr.write(`error: ${error}\n`);
      return report.ready ? 0 : 1;
    }
    if (initial.pr.body === undefined && value(argv, "--body") === undefined) throw new Error("pull request body is required for readiness publication");
    // A ready PR is still evaluated, so stale evidence cannot be hidden by an earlier transition.
    if (!report.ready || !report.packet) throw new Error(reportError(report));
    if (initial.pr.isDraft && value(argv, "--takeover") !== "true" && process.env.PATINA_READINESS_AUTOMATION !== "true") throw new Error("publishing a draft requires --takeover=true");
    const final = assess(deps.forge, repository, number, undefined, now);
    if (!final.report.ready || !final.report.packet) throw new Error(`remote changed before publication: ${reportError(final.report)}`);
    if (JSON.stringify(initial.candidate) !== JSON.stringify(final.candidate) || initial.pr.body !== final.pr.body) throw new Error(`candidate/body race: assessed ${initial.candidate.head}, remote is ${final.candidate.head}`);
    deps.store.putIfAbsent(packetKey(final.candidate, final.report.packet.bodyDigest), final.report.packet);
    deps.forge.markReady(repository, number, final.candidate.head);
    process.stdout.write(`PR #${number} is ${final.pr.isDraft ? "ready" : "already ready"} at ${final.candidate.head}\n`);
    return 0;
  } catch (error) { process.stderr.write(`pr-readiness: ${error instanceof Error ? error.message : String(error)}\n`); return 1; }
}
export function run(argv: readonly string[], deps?: ReadinessDependencies): number {
  return operation(argv, deps ?? { forge: new GhForge(process.env.PATINA_FORGE === "origin" ? "origin" : "gh"), store: new FilePacketStore() });
}
export { FilePacketStore };
