import { readFileSync } from "node:fs";
import type { BodyClaim, Observation } from "./domain.ts";
import { evaluateReadiness, parseBodyClaims } from "./evaluator.ts";
import { GhForge, FilePacketStore, candidateFromPullRequest, packetKey, type Forge, type PacketStore, type PullRequestState } from "./adapters.ts";

export interface ReadinessDependencies { readonly forge: Forge; readonly store: PacketStore; readonly observations?: (pr: PullRequestState) => readonly Observation[]; readonly bodyClaims?: (pr: PullRequestState) => readonly BodyClaim[]; readonly rules?: readonly import("./domain.ts").ObligationRule[]; }
function value(argv: readonly string[], name: string): string | undefined { const i = argv.indexOf(name); return i >= 0 && i + 1 < argv.length ? argv[i + 1] : undefined; }
function required(argv: readonly string[], name: string): string { const result = value(argv, name); if (!result || result.startsWith("--")) throw new Error(`${name} is required`); return result; }
function observationsFrom(argv: readonly string[], pr: PullRequestState): readonly Observation[] {
  const path = value(argv, "--observations") ?? process.env.PATINA_READINESS_OBSERVATIONS;
  if (!path) return [];
  const parsed: unknown = JSON.parse(readFileSync(path, "utf8")); if (!Array.isArray(parsed)) throw new Error("observations file must contain an array"); return parsed as Observation[];
}
function reportError(report: ReturnType<typeof evaluateReadiness>): string { return [...report.decisions.filter((d) => !["observed-current", "valid-by-equivalence", "not-applicable"].includes(d.validity)).map((d) => d.reason), ...report.bodyErrors].join("; ") || "readiness failed"; }

export function operation(argv: readonly string[], deps: ReadinessDependencies): number {
  try {
    const command = argv[0]; if (!command || !["open", "check", "publish"].includes(command)) throw new Error("usage: pr-readiness open|check|publish");
    const repository = required(argv, "--repo");
    if (command === "open") { const mode = value(argv, "--mode"); if (mode !== undefined && mode !== "draft" && mode !== "ready") throw new Error("--mode must be draft or ready"); const draft = mode ? mode === "draft" : value(argv, "--draft") !== "false"; const state = deps.forge.openPullRequest({ repository, base: required(argv, "--base"), head: required(argv, "--head"), draft }); process.stdout.write(JSON.stringify(state) + "\n"); return 0; }
    const number = Number(required(argv, "--pr")); if (!Number.isInteger(number) || number < 1) throw new Error("--pr requires a positive integer");
    const initial = deps.forge.readPullRequest(repository, number);
    const observations = deps.observations?.(initial) ?? observationsFrom(argv, initial);
    const body = value(argv, "--body") ?? initial.body;
    if (command === "publish" && body === undefined) throw new Error("pull request body is required for readiness publication");
    let bodyClaims: readonly BodyClaim[] | undefined;
    try { bodyClaims = deps.bodyClaims?.(initial) ?? (body === undefined ? undefined : parseBodyClaims(body)); } catch (error) { throw new Error(error instanceof Error ? error.message : String(error)); }
    const report = evaluateReadiness({ candidate: candidateFromPullRequest(initial), observations, bodyClaims, body, now: value(argv, "--now"), rules: deps.rules });
    if (command === "check") { process.stdout.write(JSON.stringify(report) + "\n"); return report.ready ? 0 : 1; }
    // A ready PR is still evaluated, so stale evidence cannot be hidden by an earlier transition.
    if (!report.ready || !report.packet) throw new Error(reportError(report));
    if (initial.isDraft && value(argv, "--takeover") !== "true" && process.env.PATINA_READINESS_AUTOMATION !== "true") throw new Error("publishing a draft requires --takeover=true");
    const finalState = deps.forge.readPullRequest(repository, number);
    const finalReport = evaluateReadiness({ candidate: candidateFromPullRequest(finalState), observations, bodyClaims: finalState.body === body ? bodyClaims : (finalState.body === undefined ? undefined : parseBodyClaims(finalState.body)), body: finalState.body, now: value(argv, "--now"), rules: deps.rules });
    if (!finalReport.ready || !finalReport.packet) throw new Error(`remote changed before publication: ${reportError(finalReport)}`);
    const initialCandidate = candidateFromPullRequest(initial); const finalCandidate = candidateFromPullRequest(finalState);
    if (JSON.stringify(initialCandidate) !== JSON.stringify(finalCandidate) || initial.body !== finalState.body) throw new Error(`candidate/body race: assessed ${initial.head}, remote is ${finalState.head}`);
    deps.store.putIfAbsent(packetKey(finalCandidate, finalReport.packet.bodyDigest), finalReport.packet);
    deps.forge.markReady(repository, number, finalCandidate.head);
    deps.forge.publishStatus?.(repository, finalCandidate.head, "success", "ready");
    process.stdout.write(`PR #${number} is ${finalState.isDraft ? "ready" : "already ready"} at ${finalCandidate.head}\n`);
    return 0;
  } catch (error) { process.stderr.write(`pr-readiness: ${error instanceof Error ? error.message : String(error)}\n`); return 1; }
}
export function run(argv: readonly string[], deps?: ReadinessDependencies): number {
  if (deps) return operation(argv, deps);
  const command = argv[0]; if (!command) throw new Error("usage: pr-readiness open|check|publish");
  const forge = new GhForge(process.env.PATINA_FORGE === "origin" ? "origin" : "gh");
  return operation(argv, { forge, store: new FilePacketStore() });
}
export { FilePacketStore };
