### Session pickup

**You own the resume point. Read the prior trail, don't redo it.** For "take over this", "resume this conversation", "continue from <transcript path>", "you're taking over", "pick up where X left off", a cloud-agent URL handoff, or a pushed branch you're meant to continue.

A pickup is inheritance. The prior agent already paid the cost of reading the code, running the repros, making the design choices. Redoing loses the bias check and burns context. Resist the urge to re-derive; read.

1. Locate the prior trail. A Claude session ID, `codex://threads/<id>` deeplink,
   or T3 Code thread ID routes through the **move-session-here** skill. For an
   explicit local transcript path, stay within the named workspace's Claude
   Code directory under `~/.claude/projects/`; crossing into another workspace
   reads unrelated private chats. A cloud-agent URL or pushed branch remains
   its own trail. Read metadata and the last messages first, then scan back for
   decision points. Parse a long transcript in a subagent and keep the reduced
   timeline in the main thread (the **principle-guard-the-context-window**
   skill).
2. Reconstruct operational state. The branch and worktree, what already landed (`git log`, `git diff` against the base), the open todos, the decisions made. The prior trail is authoritative input. Resist the bias to re-derive it.
3. Diff done vs pending. Compare what shipped against what was planned, name the resume point, do not re-run the prior repro or redo completed work. A "let me verify from scratch" pass is the tell that you're treating the trail as untrustworthy when it's actually authoritative.
4. Before state-changing work resumes, follow
   [`../references/issue-handoff.md`](../references/issue-handoff.md) with the
   `scripts/issue-routes/session-pickup.ts` entry. Run **working-on-issues**
   when the recovered state resolves one issue. Record `no-issue` when it
   resolves none. The entry must return `continue` before the next playbook
   changes state.
5. Route the remaining work to the matching playbook and pick the verdict: continue the execution, ship a finished recommendation, ratify or override a prior conclusion, or postmortem a failed run. The pickup playbook ends here; the routed playbook owns the rest.
6. Verify the inherited claims against the original goal on the real artifact (the **principle-prove-it-works** skill). A passing prior self-report is not the proof.

**Reply:** where the prior agent stopped, what you inherited vs redid (ideally nothing redone), the resume point, and the outcome.
