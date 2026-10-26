#!/usr/bin/env node
// Re-read one issue's live state before a triage write.
//
//   node check-issue-state.mjs <issue> --state <label|none> [--state-labels a,b,...]
//
// <issue> is #N or OWNER/REPO#N or a GitHub issue or pull request URL (read
// with gh), or a Linear identifier such as PAT-12 or a Linear issue URL (read
// with LINEAR_API_KEY). --state names the Triage label the pass read; `none`
// means the issue had no Triage label. --state-labels lists the tracker's
// Triage label strings, defaulting to the team skills' Triage labels.
//
// Exit 0 and print `proceed: ...` when the issue is open and its only state
// label is the expected one. Exit 2 and print `skip: ...` when it closed or its
// Triage label changed. Exit 1 when the state cannot be read; make no write.
import { spawnSync } from "node:child_process";
import process from "node:process";

const usage = "usage: check-issue-state.mjs <issue> --state <label|none> [--state-labels a,b,...]";
const defaultStateLabels = ["Needs triage", "Needs info", "Ready for agent", "Ready for human", "Won't fix"];

function fail(message) {
  console.error(`check-issue-state: ${message}`);
  process.exit(1);
}

function parseArgs(argv) {
  const args = { stateLabels: defaultStateLabels };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--state") args.state = argv[++i];
    else if (arg === "--state-labels") args.stateLabels = (argv[++i] ?? "").split(",").map((label) => label.trim()).filter(Boolean);
    else if (args.ref === undefined && !arg.startsWith("--")) args.ref = arg;
    else fail(`unexpected argument: ${arg}\n${usage}`);
  }
  if (!args.ref || !args.state) fail(usage);
  if (!args.stateLabels.length) fail("--state-labels needs at least one label");
  if (args.state !== "none" && !args.stateLabels.some((label) => label.toLowerCase() === args.state.toLowerCase())) fail(`--state ${args.state} is not one of the Triage labels: ${args.stateLabels.join(", ")}`);
  return args;
}

function parseRef(ref) {
  let match;
  if ((match = ref.match(/^https:\/\/github\.com\/([^/]+\/[^/]+)\/(?:issues|pull)\/(\d+)/))) return { provider: "github", repo: match[1], number: match[2] };
  if ((match = ref.match(/^([\w.-]+\/[\w.-]+)?#(\d+)$/))) return { provider: "github", repo: match[1] ?? "{owner}/{repo}", number: match[2] };
  if ((match = ref.match(/^https:\/\/linear\.app\/[^/]+\/issue\/([A-Z][A-Z0-9]*-\d+)/))) return { provider: "linear", id: match[1] };
  if ((match = ref.match(/^[A-Z][A-Z0-9]*-\d+$/))) return { provider: "linear", id: match[0] };
  fail(`cannot tell the tracker from ${ref}\n${usage}`);
}

// The issues endpoint serves pull requests too, and a merged one reads closed.
function readGitHub({ repo, number }) {
  const result = spawnSync("gh", ["api", `repos/${repo}/issues/${number}`], { encoding: "utf8" });
  if (result.error) fail(`cannot run gh: ${result.error.message}`);
  if (result.status !== 0) fail(`gh could not read ${repo}#${number}: ${result.stderr.trim()}`);
  const issue = JSON.parse(result.stdout);
  return { open: issue.state === "open", labels: issue.labels.map((label) => label.name) };
}

async function readLinear({ id }) {
  const key = process.env.LINEAR_API_KEY;
  if (!key) fail("LINEAR_API_KEY is not set, so the Linear issue cannot be read");
  const response = await fetch(process.env.LINEAR_API_URL ?? "https://api.linear.app/graphql", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: key.startsWith("lin_api_") ? key : `Bearer ${key}` },
    body: JSON.stringify({ query: "query($id: String!) { issue(id: $id) { state { type } labels { nodes { name } } } }", variables: { id } }),
  }).catch((error) => fail(`cannot reach Linear: ${error.message}`));
  const body = await response.json().catch(() => fail(`Linear answered ${response.status} without JSON`));
  if (!body.data?.issue) fail(`Linear could not read ${id}: ${JSON.stringify(body.errors ?? body)}`);
  const { state, labels } = body.data.issue;
  return { open: !["completed", "canceled"].includes(state.type), labels: labels.nodes.map((label) => label.name) };
}

const args = parseArgs(process.argv.slice(2));
const ref = parseRef(args.ref);
const live = ref.provider === "github" ? readGitHub(ref) : await readLinear(ref);
// Both trackers match label names without regard to case.
const stateLabels = new Set(args.stateLabels.map((label) => label.toLowerCase()));
const current = live.labels.filter((label) => stateLabels.has(label.toLowerCase()));
const expected = args.state === "none" ? [] : [args.state.toLowerCase()];
const describe = (labels) => (labels.length ? labels.join(", ") : "none");

if (!live.open) {
  console.log(`skip: ${args.ref} is closed`);
  process.exit(2);
}
if (current.map((label) => label.toLowerCase()).join() !== expected.join()) {
  console.log(`skip: ${args.ref} Triage label is now ${describe(current)}, not ${args.state}`);
  process.exit(2);
}
console.log(`proceed: ${args.ref} is open with ${args.state === "none" ? "no Triage label" : `Triage label ${args.state}`}`);
