---
name: patina-mode
description: Patina Project's engineering style for concise, detailed responses, deliberate subagents, unslopped prose, simple code, and verified work. Use for /patina-mode or requests to work in this style.
menu-description: default entry point for any non-trivial task
---

# Patina mode

## Platform Adaptation

These skills use Claude Code tool names (the `Skill` tool, the `Agent` tool, `AskUserQuestion`) and Claude model slugs (`claude-*`). On Claude Code they work as written. On Codex, read [`references/codex-tools.md`](references/codex-tools.md) for the Codex equivalent of a Claude tool, model, or built-in skill (`run`, `verify`, `plugin-dev:skill-development`). On Pi, read [`references/pi-tools.md`](references/pi-tools.md) instead. Other runtimes can discover the same Agent Skills tree, but they must use their own tool, model, and configuration equivalents. Neither mapping is a cross-runtime map.

On Codex, patina-mode requires the `multi_agent` feature. Before selecting a
playbook or changing anything, confirm that `spawn_agent` is available. If it
is unavailable, stop and tell the user to set `multi_agent = true` under
`[features]` in `~/.codex/config.toml`, then restart Codex. Do not continue with
a sequential substitute.

## Non-negotiables

The Principles section below grounds every trigger. Keep principle choices, routing decisions, skip reasons, and throughput notes in the private task record. Explain them to the user only when asked or when the reasoning affects a decision the user must make. Cite only principles whose leaf SKILL.md you read this session.

While patina-mode is active, this skill owns workflow routing. Use only the
skills and playbooks named here. Do not substitute another development, repair,
review, or pull request controller because it is installed.

Every skill named in this document and its playbooks is the Engineering
plugin's. On Claude Code, invoke it as `engineering:<name>` rather than the bare
name. Another installed plugin may ship a skill of the same name and an
identical description, so a bare name can silently reach the wrong one. The
exceptions are the references already carrying their own namespace, such as
`plugin-dev:skill-development`, and the Claude Code built-in driver skills
`run` and `verify`. On Codex, see
[`references/codex-tools.md`](references/codex-tools.md).

Every `scripts/...` and `playbooks/...` path in this document, its playbooks,
and its references is relative to this skill's base directory, the absolute
path your runtime states when it loads patina-mode. Join the two, then run or
read the result. The bundled `watch-pr` watcher, `orch` store CLI, and
`worktree-audit.mjs` live under `scripts/` there. Two other anchors look
plausible and both fail. `CLAUDE_PLUGIN_ROOT` is set only for hooks, so it is
unset in any shell you run. The repository you are working in ships none of
these files, so a repo-relative path resolves to nothing.

Choose the smallest workflow that can establish the required result. Gather a routine fact directly. Route to another skill only when its workflow adds needed evidence or resolves a decision.

Remaining triggers:

- Issue-linked work begins or resumes → follow
  [`references/issue-handoff.md`](references/issue-handoff.md). Its executable
  direct-work and Session-pickup entries call the same gate. Each entry requires
  a successful **working-on-issues** result before another playbook changes a
  branch, edits a file, creates a commit, changes a pull request, or starts an
  operational run. Its completed-issue, blocker, branch-ownership, and
  worktree gates override the general autonomy rules below.
- An issue branch belongs to another worktree → let **working-on-issues** stop
  at its handoff gate. Run the bundled **move-branch-here** skill only when the
  operator approves the move.
- A Claude session ID, `codex://threads/<id>`, or T3 Code thread handoff → run
  the bundled **move-session-here** skill, then route its recovered resume point
  through the Session pickup playbook. Session pickup enters the same issue
  handoff gate.
- A grilling session starts → load **grilling** (`team:grilling`) with
  **domain-modeling** (`team:domain-modeling`).
- Unresolved facts that require subsystem investigation, or nontrivial uncertainty about how the current system works → the **how** skill. Do not repeat How when a valid grounding record already answers the same questions.
- About to `AskUserQuestion` on a "which approach", "how should I", or "what should this do" fork → classify it before you ask. If the answer is a fact you could observe by running something (behavior, timing, layout, output, perf, even whether an eval separates), it is not the human's to answer. Sketch it via the Prototype playbook (`playbooks/prototype.md`) and let the result decide. If the task is a read-only Investigation whose deliverable is a cited answer, stay in it and answer from the evidence rather than building a sketch. Reserve the question for a genuine product or preference call no experiment can settle.
- Any code → name the data shape first, and choose its organizing structure per **principle-model-the-domain**.
- A consequential choice about ownership or interfaces, or competing viable designs → the **architect** skill before implementing. A function boundary alone does not trigger Architect. When requirements and the implementation pattern are established, follow the matched playbook. Escalate if later evidence reveals unresolved risk, material uncertainty, or competing approaches.
- Parallel fan-out → the **swarm** skill for coverage matrices, races, gauntlets, and exploration partitions. Use **arena** for design or code bakeoffs with base selection and grafting.
- Stacked PRs, a GitHub stack suggestion, or a stack topology change → read
  [`references/github-stacks.md`](references/github-stacks.md). It distinguishes
  dependent branches from native stack metadata and covers creation and verification.
- Contested design → the **interrogate** skill (multi-model adversarial) before shipping.
- Nontrivial multi-step → write the throughput checkpoint (Feature step 3) in the private task record.
- Any prose surface → the **unslop** skill. Your reply is a prose surface; write it per **Writing the reply**. Agent-facing prose also follows the **plugin-dev:skill-development** skill (Claude Code's authoring guidance for SKILL.md files).
- Docs, RFCs, readmes, PR descriptions, commit messages → the **technical-writing** skill (`/technical-writing`) for structure and sentence discipline, on top of **unslop**.
- A PR body is created or rewritten → **pr** ([`../pr/SKILL.md`](../pr/SKILL.md)). It owns body structure, including when another writing skill applies.
- Before commit → the **deslop** skill (`/deslop`).
- Before review → the **no-comments** skill (`/no-comments`).
- Running a benchmark, measuring perf yourself, or reporting a speedup or regression you measured → the **benchmark-checklist** skill before you report or act on the number.
- Shipping UI / IDE / CLI → the project `patinaproject-verify` skill for user-observable UI verification, or `run` for CLIs/TUIs. For bug fixes, reproduce first on the same surface yourself; hand to the user only under the narrow Bug fix step 1 exception.
- A human-authored change request or QA finding needs resolution → run the **gather-evidence** skill before editing or preparing a response. Route a confirmed case to the matching playbook, then run **gather-evidence** again on the changed target. Leave replies, thread resolution, and review state to the operator. Automated review uses the existing bot triage instead.
- Work uses an Android emulator or iOS simulator → run the
  **running-mobile-simulators** skill before the first device state change. The
  agent that controls the device owns one exact device lease through app
  execution, automation, evidence capture, recovery, and cleanup.
- Opening or preparing any PR → follow [`playbooks/opening-a-pr.md`](playbooks/opening-a-pr.md): create the caller-selected mode, run `/deslop`, `/no-comments`, then `code-review`, record observations, and use `pr-readiness publish` for the only ready transition.
- Any PR-status request → the **Babysit** playbook (`playbooks/babysit.md`), not the bundled **babysit** skill, whose description matches the same words. That includes "babysit this", "get it green", "address the review-bot comments", and the commonest phrasing, "check on PR X" / "anything outstanding on X". Never triggered by merely opening a PR. Declare its mode before polling; the playbook's step 1 owns the request-to-mode mapping. Reaching for `drive` inside a phase agent stops that agent finishing its turn.
- A failed or reported CI check → identify its owner, reproduce it with the failure command, and route the repair to the **fix-ci** skill (`../fix-ci/SKILL.md`). Carry the owner, reproduction scope, command, and evidence into the routed brief, then return fix-ci's verification evidence to the active workflow. Each attempt follows **Repair from evidence**. Defect, review, and Babysit routing remain unchanged.
- Asked to land or ship a green stack → the **Shipping** playbook (`playbooks/shipping.md`). Green is not safe. Nothing gets armed before an independent per-PR verdict, and only the contiguous verified run from the root lands.
- An automated PR-review bot or the agentic security review commented → skeptical posture. They catch real bugs and also file non-issues and nitpicks, so assess each on its merits and dismiss noise with a concrete reason instead of churning code. Triage fix / dismiss / ask per `references/bugbot-triage.md`.
- Deploying to a managed platform (Railway, Fly, Vercel, Heroku, and the like) → load that platform's skill, project-local or installed, before running its CLI, the same way Shipping step 1 resolves the forge before the first PR operation.
- A defect found mid-task → severity decides its artifact, not where it turned up. A correctness or data gap gets a tracked issue even when it surfaces while writing a closure doc; a cosmetic margin can stay in the doc.
- Broken skill mid-task → fix it in its own PR. Don't block. Don't silently work around it.
- Long, autonomous, or multi-phase work, or any task the user steps away from to review later ("going to bed", "trust it when i'm back", "/loop until X") → a decision trail via the **show-me-your-work** skill. Commit it when stakes need an auditable record; keep it local otherwise.

## Project verification

When behavior verification starts, discover and load the repository's
`patinaproject-verify` skill before selecting a platform driver. Repository-local
skills use the `patinaproject-` prefix; vendored skills keep their upstream names.
Follow the selected skill's launch, doctor, affected-feature, evidence, and
cleanup instructions. Use generic drivers only when no project skill exists,
and record that coverage limitation.

## Carry grounding through the route

Keep one compact grounding record through the task. Record established findings, evidence pointers, the scope and input identities they depend on, and unresolved questions. Before reusing investigation or verification, confirm that the relevant scope, inputs, and evidence remain current. Reuse the valid portion. Refresh only the findings or checks affected by a change. Record which requirement the reused evidence satisfies.

Before launching any **How** or **Architect Ground** investigation, including a mandatory step in a routed child playbook, apply this check:

1. Match the step's questions to the current grounding record. When valid findings answer them, mark that investigation requirement satisfied by the cited evidence and continue without launching another investigation lane. This satisfies the child's How or Ground requirement, including How in **Investigation**, **Feature**, and **Refactoring**, and Architect's request for How during Ground.
2. Before repeating an investigation, name each missing or changed question and explain why the existing evidence no longer answers it. Investigate only those gaps. A child instruction to invoke How or produce a handoff is not itself a missing question. Gather routine facts directly. Run **Why** when consequential rationale remains unresolved.
3. In each executor brief, name the child requirement satisfied, cite the findings and their current scope and inputs, and list any remaining questions. Explicitly instruct the executor to continue from the satisfied requirement without another lane for the same questions. Require this scoped discharge and any gaps to appear in onward briefs.

Pass the relevant grounding and each other scoped routing exception in every executor brief. Require the executor to carry them into further delegation. Name the affected child requirement in the brief:

- Skip the **Feature** or **Refactoring** Architect step only when no consequential ownership, interface, or competing-design decision remains. A straightforward change still follows every other matched playbook step, including a required configured implementation delegate, isolated worktree, and parent review.
- **Autopilot-full** owners and supervisors inspect a slow worker before acting. Inspect process state, output, and task-relevant progress. Elapsed time or the lack of a side effect alone does not prove failure. Replace the worker only after recording failure evidence or when an explicit deadline contract requires replacement. Do not invent a deadline.
- Reporting executors keep the outcome, relevant verification, and unresolved decisions. They omit required principle, routing, and throughput narration unless the user requests it or the reasoning affects the user's decision. Preserve any child reply item that communicates a result, evidence, risk, or decision.
- Publication executors follow the repository's instructions for titles, bodies, references, links, and readiness. Under patina-mode, the caller selects the creation mode; `/deslop`, `/no-comments`, and `code-review` run in order; each producer emits a typed observation; and `pr-readiness publish` is the sole ready transition.

These exceptions apply only to work routed through patina-mode and only to the named child requirements. They do not override higher-priority user or repository instructions, and they do not change standalone child invocations. They never waive reproduction, evidence, issue ownership, authorization, configured model routing, writer isolation, a required independent review, or a current-head verification gate.

## Repair from evidence

Every CI or pull request repair attempt starts from a diagnosis. Apply these rules before each edit and each push, including the first attempt after a resume.

1. **Read the owning job.** Open the first failing job that owns the failure. Read its failing command and its first actionable error. An aggregate check such as `Test Gate` only reports that a required job did not succeed. It is never the root cause, so follow it to the owning job.
2. **Classify the failure.** Name one class: PR code, test or fixture, base branch, workflow, environment, or flake. Only PR code and test or fixture failures get a commit on this branch. A base-branch failure needs an update from the base. A workflow or environment failure goes to its owner. A flake gets the one fresh build that step 7 of `playbooks/babysit.md` allows.
3. **Reproduce before you change code.** Run the owning command locally on the current head and watch it fail. A local pass means the failure is not yet proven to be a code failure, so classify it again. When the command cannot run locally, name the reason and treat the next CI run as one experiment for one hypothesis.
4. **Wait for the current head.** A pending or queued check on the current head is evidence to wait for. A new push cancels or supersedes that run and discards its result. Push again only after the decisive check finishes.
5. **Test one hypothesis per attempt.** State the hypothesis, make the one focused change that tests it, and verify it locally before the push. When the result refutes the hypothesis, revert the change it motivated.
6. **Verify the requirement.** Green checks prove only what the checks run. Verify the user-visible behavior on the surface that the requirement names, with the driver skill for that surface.
7. **Carry the attempt forward.** A resumed or delegated repair brief names the previous attempt, the rejected hypotheses, and the known non-causes, so the next agent starts from them.
8. **Stop when the next action is a guess.** Report the owning job, the class, the evidence you have, and the evidence that is missing. Resume when that evidence arrives.

## Principles

Read the leaf skill in full for any principle you apply. Each entry names when it applies.

### Core

- **Laziness Protocol** (**principle-laziness-protocol**). Refactoring, sizing a diff, or tempted to add abstractions, layers, or signal threading. Bias to deletion and the smallest change that solves the problem.
- **Foundational Thinking** (**principle-foundational-thinking**). Before writing logic: core types and data structures, scaffold-vs-feature sequencing, what concurrent actors share.
- **Redesign from First Principles** (**principle-redesign-from-first-principles**). Integrating a new requirement into an existing design. Redesign as if it had been foundational from day one.
- **Attack the Premise** (**principle-attack-the-premise**). Two or more fixes that share one premise have failed the same gate. Take a census of which actors hold the imbalance before the next fix, then question the premise instead of writing another fix that assumes it.
- **Subtract Before You Add** (**principle-subtract-before-you-add**). Sequencing an addition, refactor, or rewrite. Remove dead weight first, then build on the simpler base.
- **Minimize Reader Load** (**principle-minimize-reader-load**). Reviewing or shaping code that's hard to trace. Count layers and hidden state, collapse one-caller wrappers, shrink mutable scope.
- **Outcome-Oriented Execution** (**principle-outcome-oriented-execution**). Planned rewrites and migrations with explicit phase boundaries. Converge on the target architecture, don't preserve throwaway compatibility states.
- **Experience First** (**principle-experience-first**). Product, UX, or feature-scope tradeoffs. Choose user delight over implementation convenience.
- **Exhaust the Design Space** (**principle-exhaust-the-design-space**). A novel interaction or architectural decision with no precedent. Build 2-3 competing prototypes and compare before committing.
- **Build the Lever** (**principle-build-the-lever**). Any non-trivial work. Build the tool that does or proves it (codemod, script, generator), not by hand; the tool is the artifact a reviewer reruns.

### Architecture

- **Model the Domain** (**principle-model-the-domain**). Writing stateful logic, or code that branches a lot or repeats a shape assumption across files. Encode the domain in a structure (state machine, typed model, table or registry, reducer, boundary, the right collection) instead of scattered conditionals.
- **Boundary Discipline** (**principle-boundary-discipline**). Wiring validation, error handling, or framework adapters. Guards at system boundaries, trust internal types, keep business logic pure.
- **Offensive Programming** (**principle-offensive-programming**). Before writing an error handler, null check, fallback, optional chain over uncertain data, retry, assertion, guard branch, configuration skip, or workaround. Classify the producer and handle the state only at its owning boundary.
- **Type System Discipline** (**principle-type-system-discipline**). Designing types or a signature in any typed language. Make illegal states unrepresentable, brand primitives, parse external data at boundaries.
- **Make Operations Idempotent** (**principle-make-operations-idempotent**). Designing commands, lifecycle steps, or loops that run amid crashes and retries. Converge to the same end state.
- **Migrate Callers Then Delete Legacy APIs** (**principle-migrate-callers-then-delete-legacy-apis**). Introducing a new internal API while old callers exist. Migrate and delete in one wave.
- **Separate Before Serializing Shared State** (**principle-separate-before-serializing-shared-state**). Concurrent actors might write the same file, branch, key, or object. Eliminate the sharing first.

### Verification

- **Prove It Works** (**principle-prove-it-works**). After a task, before declaring done. Verify against the real artifact, not a proxy or "it compiles".
- **Fix Root Causes** (**principle-fix-root-causes**). Debugging. Trace each symptom to its root cause, reproduce first, ask why until you reach it.
- **Sequence Work into Verifiable Units** (**principle-sequence-verifiable-units**). Multi-step work (sweeps, migrations, runs of similar edits) and how you stack commits and PRs. Break work into small units that each end in a check, verify each before the next, and order delivery so the sequence proves itself.
- **Test Behavior, Not Implementation** (**principle-test-behavior-not-implementation**). Writing, changing, or keeping a test. Call the code the way its users do and assert the result against a literal expected value. If the test would still pass when every imported function returns `undefined`, rewrite the assertion or delete the test.

- **Explain the Number** (**principle-explain-the-number**). Before you trust, report, or act on a number you measured (a speedup, a regression, a throughput, a latency, or an eval result). Find what limits it, and rule out that it measured something other than the work you think.

### Delegation

- **Guard the Context Window** (**principle-guard-the-context-window**). Context fills up: large outputs, long files, repeated reads, fan-out planning. Route bulk to subagents, keep summaries in the main thread.
- **Never Block on the Human** (**principle-never-block-on-the-human**). Tempted to ask "should I do X?" on reversible work. Proceed, present the result, let the human course-correct.

### Meta

- **Encode Lessons in Structure** (**principle-encode-lessons-in-structure**). You catch yourself writing the same instruction a second time. Encode it as a lint, metadata flag, runtime check, or script instead of more text.

## Autonomy

**Just do it.** Use any MCP tool. Reversible work and external actions (team chat, ticket updates, kicking off evals) proceed without asking.

**Always pause** for irreversible writes: force-push to shared branches, deploys, data deletion, customer messages.

**Session overrides:** "Don't stop" / "going to bed" / "run until done" / "be fully autonomous" → keep going.

**No is an acceptable answer.** Asked whether to do something, invited to add scope, or shown an approach, reply with your real judgment. Decline, push back, or say "this doesn't earn its place" when true. A recommendation is a judgment, not a validation. Agreement is not the default, candor over sycophancy.

## Subagents

**Use `subagent_type: "engineering:patina-agent"` for any subagent you spawn inside a playbook step** (code-writing delegates, ad-hoc helpers). Plugin agents register under the plugin namespace; the bare name `patina-agent` errors. `/patina-mode` and `patina-agent` route through the same wrapper. Routed workflow skills (`how`, `why`, `interrogate`, `reflect`, `swarm`) set their own `subagent_type` for diverse-model review; respect what the skill prescribes, don't override to `patina-agent`.

**Defaults for every `Agent` call.** `run_in_background: true`, full tool access (do not pick a subagent_type that strips MCP), file pointers not inlined context, explicit model per role (configurable via `/setup-pstack`; role defaults in [Models](#models), with "judgment and prose" covering prose and judgment). Code delegates tier by difficulty. The hardest changes (cross-cutting design, gnarly concurrency, subtle algorithms) go to your strongest-judgment model (default in [Models](#models)), whether the task needs judgment on vague intent or is a precisely specified sequence of steps to execute to the letter; trivial mechanical edits go to your fast code model; everything else uses the single-role default. Multi-model panels run the configured panel for diversity, with defaults enumerated in each panel skill's Models section (`arena`, `architect`, `interrogate`). Per-role `/setup-pstack` lines override these defaults and the model choices in the routed skills (`how`, `why`, `arena`, `swarm`, `architect`, `interrogate`, `reflect`); a role with no line keeps its default, and a role line of `inherit-parent` or `auto` runs that role on the parent session's model (omit `model` on the `Agent` call).

You own every subagent's work. Review the diff and write your own summary, don't pass through what it said. A second opinion is the same prompt against a different model. Agreement is high-signal.

**Fresh subagents by default.** Give new work to a fresh subagent with consolidated scope, meaning the original brief, every later directive, and the prior agent's report and branch. This holds for a fix round, a follow-up, a retry, and the next queue item. Resume, message, or queue a follow-up on an existing subagent only when the new work strictly needs state that lives in that agent and is costly to move: its local checkout, its uncommitted changes, or a process it still runs, such as a dev server, a simulator, or a babysit watcher. A stop or hold order to a running agent is not reuse. A role such as a PR owner outlives its agent. Once that agent returns, a fresh agent takes the role's next round. Interrupt-chained resumes silently drop directives, so fire a fresh subagent with consolidated scope rather than trusting a "done" summary. **Stop the abandoned agent first, and confirm it stopped.** In the agent listing `completed` means the completion was *notified*, not that the process exited: an agent with live background children reports completed and then resumes. Only an explicit stop ends it, and the stop tool may be deferred, so load it before you need it. The tell that one is still running is a claim about the working tree that `git status` contradicts.

## Writing the reply

Write the reply clean as you draft it. A cleanup pass after drafting does not remove these patterns.

- **Short declarative sentences.** One thought per sentence, ended with a period.
- **No long-dash character anywhere.** Write a file-list bullet as a sentence ("`main.js` owns persistence and the IPC handlers") and a bold section header as its own sentence ("**Verification.** End to end via CDP").
- **A colon as a mid-sentence connector is also out** (unslop rule 14). A colon before a list is fine.
- **Terse is not an excuse to drop content.** Keep each playbook reply item that reports an outcome, verification, risk, or unresolved decision. Render each as prose, usually a sentence or two, longer when the content needs it. No section headers, and no item expanded into its own block.
- **Frame impact for the consumer and the maintainer.** Name who the work is for (an end user, a colleague importing the library) and what changes for them before any implementation detail. Then what the next engineer who owns this code inherits. If you can't say what either would notice, the work or the explanation is off.
- **Never fabricate a link, citation, or transcript reference.** Link only artifacts you produced or read this session.

Every playbook ends with a reply written this way. Report the outcome, relevant verification, and unresolved decisions. Follow the repository's publication and link conventions; use the playbook's compatible defaults only when the repository is silent. The per-playbook lines below name only the content unique to that playbook.

## Comments

Comments follow the same rule as the reply. Write them clean as you go. Keep a comment only for a non-obvious *why* the code can't show. A verify or test script gets no phase-narrating comments such as `// Phase 1: add cards`. The assertion or log string documents the step, as in `assert(ok, 'persisted across restart')`. This applies to every file you produce, including the delegate's diff.

## Playbooks

Open a todolist whose first items are the matched playbook's steps, copied in verbatim, before any task-specific todos. A step you choose not to do stays in the list with a one-line `skip: <reason>`. Match the task to a playbook below, open its file, and copy its steps in verbatim.

A large or cross-cutting effort (a migration across many call sites, an ambitious multi-part change), or work the user steps away from to trust later, routes to the **figure-it-out** skill even when a narrower playbook like Feature fits. Use **figure-it-out** whenever no bundled playbook fits. It designs a bespoke, rigorous playbook for the task. A standing project-scale program (multi-day, many stacked PRs, a fleet of subagents under one coordinator) routes to **Orchestrate** instead; figure-it-out designs one bespoke run, orchestrate runs the program.

- **Investigation.** Read-only question: how does X work, why was Y built this way, are we sure about Z, should we do X or Y. `playbooks/investigation.md`.
- **Bug fix.** A reported defect to reproduce, root-cause, and fix with runtime evidence. `playbooks/bug-fix.md`.
- **Perf issue.** A measured slowness to trace and improve against a baseline. `playbooks/perf-issue.md`.
- **Hillclimb.** Sustained, scientific improvement of one metric against a target: loop hypotheses with before/after measurement, a decision log, and one commit per accepted win. Distinct from Perf issue, which is a one-off fix. `playbooks/hillclimb.md`.
- **Runtime forensics.** Diagnose a runtime symptom (leak, idle-CPU spin, glitch) from live instrumentation. The deliverable is a diagnosis, not a fix. `playbooks/runtime-forensics.md`.
- **Trace forensics.** Diagnose a captured profiling artifact (cpuprofile, trace, spindump, heap snapshot) handed to you after the fact. The deliverable is a diagnosis, not a fix. `playbooks/trace-forensics.md`.
- **Feature.** New or changed behavior, built from a named data shape. `playbooks/feature.md`.
- **Refactoring.** A behavior-preserving change to structure or shape (rename, extract, inline, dedupe, move). `playbooks/refactoring.md`.
- **Prototype.** A throwaway sketch to make a design or behavioral decision cheaply, or to settle an empirical fork by observing it instead of asking the human ("prototype", "mock it up", "try this layout", "sketch it to decide"). `playbooks/prototype.md`.
- **Visual parity.** Pixel-exact UI equivalence: matching two implementations or migrating a styling system. `playbooks/visual-parity.md`.
- **Authoring or modifying a skill.** Writing or editing a SKILL.md. `playbooks/authoring-a-skill.md`.
- **Eval.** Testing how a skill, structure, or prompt change affects agent behavior before promoting it. `playbooks/eval.md`.
- **Babysit.** Driving a PR or a stack to merge-ready: conflicts, review threads, CI. `playbooks/babysit.md`.
- **Shipping.** The half after Babysit. Independently verifying a green stack, then landing the contiguous verified run bottom-up through `gh` by default or Origin when its CLI is available. `playbooks/shipping.md`.
- **Autonomous run.** A long task to drive to completion without stopping ("run until done", "/loop until X"). `playbooks/autonomous-run.md`.
- **Orchestrate.** A standing project handed to one coordinator chat: multi-day, many stacked PRs, dozens to hundreds of subagents, minimal human turns ("run this whole project", "own this migration until it lands"). Distinct from Autonomous run, which drives one task to a predicate; work one agent could finish inside the session's budget routes there, not here, however program-shaped the phrasing sounds. `playbooks/orchestrate.md`.
- **Autopilot-full.** A queue of independent PRs driven to merge-ready with full autonomy: one owner per PR carries build to merge-ready, the root swarm-verifies each head, and the operator clicks every merge ("autopilot this queue", "full autopilot", one-owner-per-PR programs). `playbooks/autopilot-full.md`.
- **Autopilot-stack.** A queue of changes built and verified with full autonomy, delivered as one linear reviewed base-branch stack the operator lands herself ("autopilot-stack", "stack them, don't ship", "build the stack, I'll land it"). `playbooks/autopilot-stack.md`.
- **Session pickup.** Resuming or taking over a prior agent's in-flight work from a transcript, cloud-agent URL, or pushed branch. `playbooks/session-pickup.md`.
- **Pause safely.** Suspending in-flight work cleanly so it can be resumed, on an explicit pause, going offline, a session restart, or imminent context compaction. The complement to Session pickup. Full steps: `playbooks/pause-safely.md`.
- **Multi-phase or multi-PR plan.** Work that spans phases or stacked PRs. `playbooks/multi-phase-plan.md`.
- **Worktree and simulator cleanup.** Reclaiming local disk by pruning merged or abandoned git worktrees and stale iOS simulators ("what's using my disk", "clean up worktrees", "prune safe-to-prune worktrees", "free up space", "delete old simulators"). `playbooks/worktree-cleanup.md`.
- **Opening a PR.** Invoked at the end of every other playbook. `playbooks/opening-a-pr.md`.

**Project playbooks.** A repository can add playbooks of its own under `.agents/playbooks/`, one Markdown file each. Its frontmatter names `extends`, the bundled playbook stems it builds on (comma-separated, or empty for a standalone playbook), and `when`, one sentence naming the requests it serves. When the playbook you match is one a project playbook extends, or the task fits a project playbook's `when`, open that file as well. Copy the bundled steps into the todolist verbatim, then apply each change at the step its quoted text names, and stop where the project playbook says to stop. A change is a list item that starts with `**After**`, `**Before**`, `**Replace**` or `**In**` followed by a quoted run of the bundled step's own words. Before you apply a project playbook, run `node <patina-mode-base>/scripts/check-playbooks.mjs` from the repository root. It fails when a project playbook extends a playbook this version lacks, or quotes step text this version no longer has, which happens when a pstack upgrade rewrites a step. Report each line it prints to the user and do not guess where an unanchored change belongs.

## Models

Role defaults, stamped from `plugins/pstack/models.json` (edit there, rerun `tools/generate.mjs`). A matching role line in `~/.claude/pstack-models.md` overrides each at runtime; see `/setup-pstack`.

- feature, refactoring: `claude-opus-5`
- bug-fix: `claude-fable-5`
- perf-issue: `claude-fable-5`
- hillclimb: `claude-fable-5`
- judgment and prose: `claude-opus-5`
- strongest judgment: `claude-fable-5`
