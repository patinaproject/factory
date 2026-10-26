# Readiness Check

The readiness check is the only way to apply `Ready for agent` or `Ready for human`. It runs every time one of those labels would be applied: at the end of a triage pass, and when the maintainer asks for the label directly. No instruction skips it.

## The checklist

Every item must hold:

1. **Type.** Exactly one Type label from the repository's Type set is set. A Theme label is set when the evidence supports one; a missing Theme never fails this item. The issue-tracker config's **Labels** section defines both.
2. **Claim verified.** The claim is verified the way the Type's row in the issue-tracker config says: a reproduction names the code path, a redundancy check confirms it is not already built and records where you looked, a one-off action is confirmed not already done, and a design request is confirmed not already covered by an existing design. For a PR, the diff does what it claims.
3. **Out of scope checked.** `.out-of-scope/*.md` was read, and any match is named and addressed. "None match" is a valid result.
4. **Acceptance criteria and scope boundaries.** Concrete, independently verifiable acceptance criteria are written, and so is what is out of scope.
5. **No open questions.** The triage notes list nothing still needed.
6. **Reason it can't be delegated.** For `Ready for human` only: the notes say why an agent can't do it.
7. **Decisions approved.** Every design or naming decision the brief depends on that meets all three ADR criteria is listed in the triage notes, with the person who approved it:
   - **Hard to reverse:** changing your mind later costs something meaningful.
   - **Surprising without context:** a future reader would wonder why it was done this way.
   - **The result of a real trade-off:** there were genuine alternatives.

   An agent's recommended answer counts only once a person accepts or overrides it. An agent never approves a decision. "None needed" is a valid result.

## Record it

Keep the checklist in the triage notes, so the next session sees what passed and what didn't:

```markdown
**Readiness check:**

- [x] Type: Tools; Theme: Deliverability & reliability
- [x] Claim verified: not built; searched the `SkillConfig` loader and the scheduler
- [x] Out of scope checked: no match
- [ ] Acceptance criteria and scope boundaries: out-of-scope list missing
- [ ] No open questions: 1 open
- [x] Reason it can't be delegated: n/a (Ready for agent)
- [ ] Decisions approved: 1 awaiting approval
```

List each qualifying decision under the checklist with its answer and who approved it, or mark it awaiting approval. Record a qualifying decision only here, never under "still needed":

```markdown
**Decisions:**

- Name the label `Ready for human`, not `Needs human`: approved by @person
- Store briefs as issue Documents, not comments: awaiting approval
```

Write the triage notes and the agent brief through the issue-tracker config's issue-artifact operation. When it defines none, post each as a comment.

## Outcome

- **Every item holds:** write the agent brief from what the checklist established, then apply the label. When the Type needs no repository change, such as `Task`, the brief names the action to take and how to confirm it is done, not code changes.
- **An item fails:** do not apply the label. Close each gap you can yourself: reproduce the report, search the codebase, read `.out-of-scope/`. Grill only about the gaps that remain, in the grilling skill's usual format. Ask nothing the checklist already settled. Record the remaining questions under "still needed" in the triage notes. If only the reporter can answer them, apply `Needs info`; otherwise leave `Needs triage`.
- **"Decisions approved" fails:** follow the bullet above, but never close this gap yourself. Ask each unapproved decision as a grilling question with your recommended answer. The item fails until a person answers it.
