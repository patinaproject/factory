import { accessSync, readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { join } from "node:path";
import { MemoryPacketStore, markReadyIfCurrent } from "../../../pr-readiness/scripts/adapters.ts";
import { operation } from "../../../pr-readiness/scripts/cli.ts";
import type { ForgeAdapter, PullRequestHead, ReviewRecord } from "./types.ts";
import { compatibilityRules, reviewRecordObservations } from "./state.ts";

function getReviewRoot(): string {
  return (
    process.env.PATINA_CODE_REVIEW_ROOT ??
    join(process.env.TMPDIR ?? "/tmp", "code-review")
  );
}

function parseNumber(value: string | undefined): number {
  const number = Number(value);
  if (!Number.isInteger(number) || number < 1)
    throw new Error("--pr requires a positive integer");
  return number;
}

function parseJson(value: string): Record<string, unknown> {
  const parsed: unknown = JSON.parse(value);
  if (!parsed || typeof parsed !== "object")
    throw new Error("forge returned invalid JSON");
  return parsed as Record<string, unknown>;
}

function readRecord(path: string, readStdin: () => string): string {
  return path === "-" ? readStdin() : readFileSync(path, "utf8");
}

function readForge(
  command: "gh" | "origin",
  number: number,
  repository?: string
): PullRequestHead {
  const args = [
    "pr",
    "view",
    String(number),
    "--json",
    "headRefOid,headRefName,isDraft",
  ];
  if (repository) args.push("--repo", repository);
  const row = parseJson(execFileSync(command, args, { encoding: "utf8" }));
  const resolvedRepository = repository ?? process.env.PATINA_REPOSITORY;
  if (
    typeof row.headRefOid !== "string" ||
    typeof row.headRefName !== "string" ||
    typeof resolvedRepository !== "string"
  )
    throw new Error("forge response lacks repository, branch, or head SHA");
  return {
    number,
    repository: resolvedRepository,
    branch: row.headRefName,
    head: row.headRefOid,
    isDraft: row.isDraft === true,
  };
}

function adapter(name: "gh" | "origin"): ForgeAdapter {
  return {
    name,
    readPullRequest: (number, repository) =>
      readForge(name, number, repository),
    markReady: (number, repository, expectedHead) =>
      markReadyIfCurrent(name, repository ?? "", number, expectedHead ?? readForge(name, number, repository).head),
  };
}

export function parseRecord(path: string, text: string): ReviewRecord {
  const head = text.match(/^Head(?: SHA)?:\s*(\S+)/m)?.[1] ?? "";
  const status = (text.match(
    /^Status:\s*(open|resolved|superseded)\s*$/m
  )?.[1] ?? "open") as ReviewRecord["status"];
  const standards =
    text.match(
      /^## Standards\s*\n([\s\S]*?)(?=^## Spec\b|^### Resolution\b|^Status:|$)/m
    )?.[1] ?? "";
  const spec =
    text.match(
      /^## Spec\s*\n([\s\S]*?)(?=^### Resolution\b|^Status:|$)/m
    )?.[1] ??
    (/Spec:\s*skipped, no linked issue\./i.test(text) ? "skipped" : "");
  const section =
    text.match(/^### Resolution\s*\n([\s\S]*?)(?=^Status:|$)/m)?.[1] ?? "";
  const resolutions = section
    .split("\n")
    .map((line) => line.replace(/^\s*-\s*/, "").trim())
    .filter(Boolean);
  const matches = [
    ...standards.matchAll(
      /^\s*-\s*Finding\s+(\S+)\s+\((hard|soft|smell|scope-creep)\):/gim
    ),
    ...spec.matchAll(
      /^\s*-\s*Finding\s+(\S+)\s+\((hard|soft|smell|scope-creep)\):/gim
    ),
  ];
  const findings = matches.map((match) => ({
    id: match[1],
    kind: match[2].toLowerCase() as "hard" | "soft" | "smell" | "scope-creep",
  }));
  const resolved = new Set(
    resolutions
      .map(
        (line) =>
          line.match(/^\S+:\s*(?:Fixed in|Dismissed:)/i)?.[0]?.split(":")[0]
      )
      .filter(Boolean)
  );
  for (const [index, resolution] of resolutions.entries())
    if (
      !/^\S+:/.test(resolution) &&
      /^(?:Fixed in|Dismissed:)/i.test(resolution) &&
      findings[index]
    )
      resolved.add(findings[index].id);
  const blockingFindings = findings.filter(
    (finding) => finding.kind === "hard" && !resolved.has(finding.id)
  ).length;
  return {
    path,
    head,
    status,
    standards,
    spec,
    findings,
    resolutions,
    blockingFindings,
    dismissedFindings: resolutions.filter((line) =>
      /^\S+:\s*Dismissed:/i.test(line)
    ).length,
  };
}

export function run(
  argv: readonly string[],
  forgeOverride?: ForgeAdapter,
  readStdin: () => string = () => readFileSync(0, "utf8")
): number {
  try {
    const prIndex = argv.indexOf("--pr");
    const pr = parseNumber(prIndex >= 0 ? argv[prIndex + 1] : undefined);
    const repoIndex = argv.indexOf("--repo");
    const repository = repoIndex >= 0 ? argv[repoIndex + 1] : undefined;
    const recordIndex = argv.indexOf("--record");
    const explicitRecord = recordIndex >= 0 ? argv[recordIndex + 1] : undefined;
    if (recordIndex >= 0 && (!explicitRecord || explicitRecord.startsWith("--"))) throw new Error("--record requires a file path or -");
    const forge = forgeOverride ?? adapter(process.env.PATINA_FORGE === "origin" ? "origin" : "gh");
    const current = forge.readPullRequest(pr, repository);
    const path = explicitRecord ?? join(getReviewRoot(), current.repository, current.branch, `${current.head}.md`);
    if (path !== "-") { try { accessSync(path); } catch { throw new Error(`no local review exists for current head ${current.head}: ${path}`); } }
    const recordText = readRecord(path, readStdin);
    const record = parseRecord(path, recordText);
    if (record.head !== current.head) throw new Error(`review head ${record.head} does not match current head ${current.head}`);
    const candidate = { repository: current.repository, pullRequest: current.number, base: "unknown", mergeBase: "unknown", head: current.head, patchId: current.head, diffDigest: current.head, requirementsDigest: "legacy", policyDigest: "legacy" };
    const observations = reviewRecordObservations(record, candidate);
    const readinessForge = {
      readPullRequest: () => ({ ...candidate, number: current.number, isDraft: current.isDraft, body: `${recordText}\n## Evidence\n${observations.map((o) => `<!-- pr-readiness: ${JSON.stringify({ observationId: o.id, obligation: o.obligation, validity: "observed-current" })} -->`).join("\n")}` }),
      openPullRequest: () => ({ ...candidate, number: current.number, isDraft: current.isDraft }),
      markReady: (_repository: string, _number: number, expectedHead: string) => forge.markReady(pr, repository, expectedHead),
    };
    const result = operation(["publish", "--repo", current.repository, "--pr", String(pr), "--takeover", "true"], { forge: readinessForge, store: new MemoryPacketStore(), observations: () => observations, rules: compatibilityRules() });
    if (result === 0) process.stdout.write(`Local code review passed for PR #${pr} at ${current.head}. ${record.dismissedFindings} dismissed finding(s).\n`);
    return result;
  } catch (error) { process.stderr.write(`mark-ready: ${error instanceof Error ? error.message : String(error)}\n`); return 1; }
}
