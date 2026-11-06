### Session pickup

**You own the resume point. Read the prior trail, don't redo it.**

1. Read the project checkpoint first using [Resume storage](../references/resume-storage.md). A successful read gives the note and artifacts without requiring a path from the previous session. An unavailable or incomplete checkpoint needs diagnosis rather than a silent fallback. If the pointer is missing, locate the prior trail. A Claude session ID, `codex://threads/<id>` deeplink,
   or T3 Code thread ID routes through the **move-session-here** skill. For an
   explicit local transcript path, stay within the named workspace's Claude
   Code directory under `~/.claude/projects/`; crossing into another workspace
   reads unrelated private chats. A cloud-agent URL or pushed branch remains
   its own trail. For transcript fallback, read metadata and the last messages
   first, then scan back for decision points. Parse a long transcript in a
   subagent and keep the reduced timeline in the main thread (the
   **principle-guard-the-context-window** skill).
2. Reconstruct operational state. The branch and worktree, what already landed (`git log`, `git diff` against the base), the open todos, the decisions made. Use the prior trail to recover progress. For claims about human rulings, follow [Claims about human decisions](../../why/references/epistemics.md#claims-about-human-decisions). An agent summary is not the original ruling.
3. Diff done vs pending. Compare what shipped against what was planned, name the resume point, do not re-run the prior repro or redo completed work. Recheck claims that affect the remaining work without repeating completed investigations. Read artifacts linked by the resume note, including requested questions or checklists.
4. Before state-changing work resumes, follow
   [`../references/issue-handoff.md`](../references/issue-handoff.md) with the
   `scripts/issue-routes/session-pickup.ts` entry. Run **working-on-issues**
   when the recovered state resolves one issue. Record `no-issue` when it
   resolves none. Choose the playbook that step 5 routes the remaining work to,
   and send it. The entry must return `continue` before the next playbook
   changes state.
5. Route the remaining work to the matching playbook and pick the verdict: continue the execution, ship a finished recommendation, ratify or override a prior conclusion, or postmortem a failed run. Check the remote canonical branch that **working-on-issues** resolved and fetched in step 4. When it has an open pull request, the matching playbook is Babysit, and Babysit step 1 chooses the mode. When it has commits beyond the default branch and no open pull request, continue with the remaining steps of the build playbook that the work matches, then **Opening a PR**. The pickup playbook ends here. The routed playbook owns the rest.
6. Verify the inherited claims against the original goal on the real artifact (the **principle-prove-it-works** skill). A passing prior self-report is not the proof.

**Reply:** where the prior agent stopped, what you inherited vs redid (ideally nothing redone), the resume point, and the outcome.
