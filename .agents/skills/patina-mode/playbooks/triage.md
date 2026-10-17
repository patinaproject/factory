### Triage

**You own the verdict. Investigate, decide, record.** The run is unattended whoever starts it. No step waits for a reply. Adapted from Lauren Tan's Benny `triage-issue-reports`.

Reach the tracker only through the repository's issue-tracker adapter, and only from the coordinator. Subagents are read-only and return findings. Every subagent prompt forbids tracker writes. When the adapter lacks an operation a step needs, skip that write and name the gap in the reply. Never improvise a tracker write.

1. **Freeze.** Record the issue identifier and repository once. Every later read and write uses them.
2. **Check idempotency and ownership.** Read the issue's state, roles, triage notes, and linked pull requests. Stop without writing when the notes already hold a verdict that no later comment or edit outdates, when a person is assigned and working on it, or when an open pull request already addresses it.
3. **Read the whole report.** Read the body, every comment, and every attachment at full resolution. Capture expected and observed behavior, environment, trigger, frequency, error text, and linked issues, commits, or pull requests. Name an attachment you cannot read as unread. Never describe it. Use evidence already in the issue before asking for more.
4. **Classify.** Decide one category role, `bug` or `enhancement`. Then sort the issue as trivial, such as a typo or a one-line change with an obvious location, or standard. Trivial issues skip steps 10 and 11 and the parallel reproduction in step 6.
5. **Trace the cause.** Run **how** over the path from the reported action to the observed result, and check whether the symptom belongs to that path or to a dependency. On standard issues, run two **why** investigators: one for regressions and earlier attempts, one for existing fixes, open work, and earlier rejections. Keep confirmed facts and hypotheses in separate lists. For an enhancement, search the codebase for an existing implementation and record where you looked.
6. **Reproduce (bugs only).** Give one subagent on the bug-fix role the trace and the report. It returns the command and output, or what it tried. Re-run a claimed reproduction yourself before you count it. When it fails and the code path is still unclear, run up to three parallel attempts with different strategies through **swarm**. Then take one of three results:
   - **Reproduced.** Record the command and output.
   - **Diagnosed, not reproduced.** The code path is named. Write out the failing test the fix should start from. The bug-fix playbook reproduces it at implementation.
   - **Neither.** The reporter must supply steps or environment. List what was tried.
7. **Check out of scope.** Read `.out-of-scope/*.md`. Name any match and how it bears on the issue. "None match" is a valid result.
8. **Check duplicates and routing.** Search the tracker for the same signature, or the same area, trigger, and symptom. Record a confident duplicate, or an owner in another repository or team, as a recommendation in the triage notes. Never close, move, relabel, or reassign.
9. **Draft the brief.** Write the agent brief with testable acceptance criteria and scope boundaries. Never present a hypothesis as a finding in the title or brief.
10. **Review the brief.** Run an **interrogate** panel of two models on the brief. Fix untestable criteria and unstated assumptions it finds.
11. **Check blast radius.** Run **blast-radius** on every standard enhancement and on any bug whose fix is unclear. Write what it finds into the brief's scope boundaries.
12. **Run the readiness check** in [`../references/readiness.md`](../references/readiness.md). Step 13 replaces its interactive outcome and grilling. Record the checklist and a Decision log in the triage notes. The log has one entry per decision you settled: what, why, the evidence, and the result.
13. **End the run.** Write the triage notes and the agent brief through the adapter's issue-artifact operation. Then end with exactly one outcome:
    - **Every item holds.** Apply the category role and the ready role.
    - **An item fails.** Apply the category role and no ready role. Ask one round of questions, each with a recommended answer. Ask only for a decision that meets all three ADR criteria, a fact only the reporter has, or a scope choice with more than one reasonable answer. Never ask for a fact you could look up. Apply `needs-info` when only the reporter can answer, and leave `needs-triage` otherwise.

Apply only category and state roles. Recommend `wontfix` in the notes rather than applying it. Model roles follow patina-mode's Models section: `how` explorers, `why` investigators, and the `interrogate` panel take their own roles, and reproduction takes the bug-fix role. No progress narration reaches the tracker.

**Reply:** the first line is `Triage outcome: <role>` when a ready role was applied, or `Needs input:` followed by the round's questions. Then the category, the reproduction result, and links to the triage notes and agent brief.
