# patina-mode eval harness

This folder holds the first, model-free slice of the eval for the factory's
patina-mode workflow (GitHub issue #579). It runs four smoke fixtures through
[Inspect AI](https://inspect.aisi.org.uk/) with scripted solutions, checks that
the trial adapter matches the real worker's construction, and reports cost and
speed.

The harness makes no model call. Inspect runs on its stand-in model,
`mockllm/model`, and a scripted solver applies each case's reference solution or
near-miss mutation. No inspect_swe agent runs. Smoke results validate plumbing
only. They are not evidence of agent reliability or model quality, and every
result file says so in its `evidence` field.

## Run it

The harness needs Python 3.14 and its pinned dependencies. Run every command
from this folder.

```sh
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.txt
.venv/bin/python -m patina_evals run --split development --suite regression --out results/smoke.json
```

`run` takes these flags:

| Flag | Meaning |
| --- | --- |
| `--split development\|holdout` | Which split to run. No case is in `holdout` yet, so it gives an empty, valid result. |
| `--suite regression\|capability\|all` | Which suite to run. Regression cases are required. Capability cases never fail the run. |
| `--out <file>` | Where to write the result. |
| `--loader <name>`, `--cases <dir>`, `--graders <dir>` | Read cases in another format from another folder. See [Loading other case formats](#loading-other-case-formats). |
| `--require-cost <path>` | Fail unless the cost at this report path is known, such as `experiment_total.cost_usd`. |
| `--concurrency`, `--cpu-seconds`, `--memory-bytes`, `--command-timeout`, `--trial-timeout` | Trial limits. The result records the values used. |
| `--log-dir <dir>` | Where Inspect writes its log. |

Two more commands exist. `parity` prints the adapter parity report, and
`validate <file>` checks a result file against the schema.

Run the harness's tests with this command:

```sh
.venv/bin/python -m unittest discover -s tests -t .
```

`pnpm test` at the repository root does not run these tests and does not need
these dependencies. The `Evals` workflow runs them on every pull request. It runs
the smoke fixtures only when the path filter says the change qualifies. It is not
a required check.

## Exit codes

| Code | Reason |
| --- | --- |
| 0 | Every required case passed. A failed capability case adds a diagnostic and still exits 0. |
| 1 | A required case failed, or a model call was recorded. |
| 2 | The command line was wrong. |
| 3 | A required case did not finish, for example a trial raised an error. |
| 4 | Adapter parity is invalid, so no comparison is valid. |
| 5 | A `--require-cost` path has unknown cost. Unknown cost is never replaced by zero. |

The result's `summary.exit_code` and `summary.exit_reason` hold the same value,
locally and in CI.

## What a run does

Each case solution becomes one Inspect sample in the `local` sandbox. A trial
gets a fresh home, temp folder, mock tracker state, and repository. The
repository holds one commit of the pre-fix state, with no remote and no other
history. Commands run with an empty environment plus an allowlist, in their own
process group, under CPU and memory ceilings. Teardown kills any process the
trial left behind. Model, tracker, and proxy endpoints point at a refusing local
address. Each trial worktree has one writer lock.

References, mutations, and graders live in `graders/` and never enter a trial.
The grader runs its probes in the trial, then grades in the harness process.
A change to a case's protected files, such as its visible tests, or to its
grader folder fails the trial as `grader_tampered`.

The local sandbox shares the host's CPU and memory, so the result records no
guarantee, only the ceilings. macOS does not enforce the memory ceiling, and the
result says so. The factory machine has no Docker. The one isolation test that
needs a Linux container, `ContainerTest` in `tests/test_isolation.py`, skips
locally and runs in the `Evals` workflow on `ubuntu-24.04`. Every other test runs
without Docker.

## Fixtures

| Case | Area | Reference | Near-misses |
| --- | --- | --- | --- |
| `triage-priority-allowed` | Triage priority | Sets Low from the rubric and posts the outcome line. | Never sets a priority. Sets Urgent. |
| `triage-priority-human-set` | Human authority | Keeps a person's High and says a person set it. | Overwrites it with Low. Posts nothing. |
| `token-spend-supported-usage` | Token accounting | Prices every usage category at its own rate. | Drops cache reads. Prices one-hour writes at the five-minute rate. Rewrites the visible test. |
| `token-spend-unknown-cost` | Token accounting | Reports an unpriced model and missing usage as unknown. | Reports zero. Claims the unknown record is complete. |

Each `fixtures/<case>/manifest.json` records versioned task text, provenance from
public sources, the pre-fix state, dependencies, scripted human and tool
responses, acceptance checks, forbidden outcomes, and a `group`. Sibling cases
share a group, so a later holdout split can move a whole group at once.

## Adapter parity

`patina_evals/adapter.py` loads `profiles/claude-worker/scripts/claude-session`
and calls the functions the worker uses to build its prompt, settings, hooks, MCP
configuration, and session plan. It builds them twice, once from the committed
profile and once for a trial's mock services. `patina_evals/parity.py` compares
the two across the worker prompt, project instructions, tool search, agents and
hooks, skill and dependency pins, role models, effort, resume behavior, and
environment. Role models compare the committed placeholder structure only.

Every intended difference is listed in `parity/differences.json`, with both
values and a reason. Any other difference, a stale entry, or a value the adapter
cannot read makes the comparison invalid.

## Cost and speed

`patina_evals/ledger.py` builds the cost report from recorded or synthetic usage
events. It reuses the token and price math in
`profiles/claude-worker/scripts/token_math.py` and the pinned
`token_prices.json`, and keeps worker, grader, and other scopes apart. Unknown
model, usage, or cache-write TTL stays unknown, with a flag. Scripted smoke runs
record no usage, so their cost is unknown and says so.

`patina_evals/speed.py` records monotonic spans for the run, each trial, and its
setup, solve, grading, and wait parts. End-to-end wall time is the run span, never
a sum of parallel trials. The report also gives calls, turns, retries, retry
delay, and the median and p95 trial duration.

## Loading other case formats

A loader is a function `(cases: Path, graders: Path) -> list[Manifest]`. It reads
a folder named at run time with `--cases`, maps each external case to the common
`Manifest`, and registers itself with `loader.register(name, function)`. Select
it with `--loader <name>`. The harness, graders, scorer, and result schema stay
the same. A loader for reviewer cases from another repository can be added this
way later. This slice ships only the interface and a test with a synthetic
folder, because that format is not public and not final.

## Path filter

`patina_evals/path_filter.py` decides whether a change qualifies for the eval.
It reads its globs from `qualifying_paths.json`, needs only the standard
library, and runs from the repository root:

```sh
python3 evals/patina_evals/path_filter.py --base origin/main --head HEAD
```
