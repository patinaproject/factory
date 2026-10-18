# Readiness Check

The readiness check is the only way to apply `ready-for-agent` or `ready-for-human`. It runs every time one of those roles would be applied: at the end of a triage pass, and when the maintainer asks for the role directly. No instruction skips it.

## The checklist

Every item must hold:

1. **Category.** Exactly one category role, `bug` or `enhancement`, is set.
2. **Claim verified.** For a bug, it is reproduced and the code path is named. For an enhancement, the redundancy check confirms it is not already built and records where you looked. For a PR, the diff does what it claims.
3. **Out of scope checked.** `.out-of-scope/*.md` was read, and any match is named and addressed. "None match" is a valid result.
4. **Acceptance criteria and scope boundaries.** Concrete, independently verifiable acceptance criteria are written, and so is what is out of scope.
5. **No open questions.** The triage notes list nothing still needed.
6. **Reason it can't be delegated.** For `ready-for-human` only: the notes say why an agent can't do it.
7. **Decisions approved.** Every design or naming decision the brief depends on that meets all three ADR criteria is listed in the triage notes, with the person who approved it:
   - **Hard to reverse:** changing your mind later costs something meaningful.
   - **Surprising without context:** a future reader would wonder why it was done this way.
   - **The result of a real trade-off:** there were genuine alternatives.

   An agent's recommended answer counts only once a person accepts or overrides it. An agent never approves a decision. "None needed" is a valid result.

## Record it

Keep the checklist in the triage notes, so the next session sees what passed and what didn't:

```markdown
**Readiness check:**

- [x] Category: enhancement
- [x] Claim verified: not built; searched the `SkillConfig` loader and the scheduler
- [x] Out of scope checked: no match
- [ ] Acceptance criteria and scope boundaries: out-of-scope list missing
- [ ] No open questions: 1 open
- [x] Reason it can't be delegated: n/a (ready-for-agent)
- [ ] Decisions approved: 1 awaiting approval
```

List each qualifying decision under the checklist with its answer and who approved it, or mark it awaiting approval. Record a qualifying decision only here, never under "still needed":

```markdown
**Decisions:**

- Name the role `ready-for-human`, not `needs-human`: approved by @person
- Store briefs as issue Documents, not comments: awaiting approval
```

Write the triage notes and the agent brief through the issue-tracker config's issue-artifact operation. When it defines none, post each as a comment.

## Outcome

- **Every item holds:** write the agent brief from what the checklist established, then apply the role.
- **An item fails:** do not apply the role. Close each gap you can yourself: reproduce the bug, search the codebase, read `.out-of-scope/`. Grill only about the gaps that remain, in the grilling skill's usual format. Ask nothing the checklist already settled. Record the remaining questions under "still needed" in the triage notes. If only the reporter can answer them, apply `needs-info`; otherwise leave `needs-triage`.
- **"Decisions approved" fails:** follow the bullet above, but never close this gap yourself. Ask each unapproved decision as a grilling question with your recommended answer. The item fails until a person answers it.
