# Readiness Check

The readiness check is the only way to apply `Ready for agent` or `Ready for human`. It runs every time one of those labels would be applied: at the end of a triage pass, and when the maintainer asks for the label directly. No instruction skips it.

## The checklist

Every item must hold:

1. **Type.** Exactly one Type label from the repository's Type set is set. A Theme label is set when the evidence supports one; a missing Theme never fails this item. The issue-tracker config's **Labels** section defines both.
2. **Claim verified.** The claim is verified the way the Type's row in the issue-tracker config says: a reproduction names the code path, a redundancy check confirms it is not already built and records where you looked, a one-off action is confirmed not already done, and a design request is confirmed not already covered by an existing design. For a PR, the diff does what it claims.
3. **Out of scope checked.** `.out-of-scope/*.md` was read, and any match is named and addressed. "None match" is a valid result.
4. **Acceptance criteria and scope boundaries.** Concrete, independently verifiable acceptance criteria are written, and so is what is out of scope.
5. **No open questions.** The triage notes list nothing still needed.
6. **Reason it can't be delegated.** For `Ready for human` only: the notes say why an agent can't do it. An open question is never that reason. It fails item 5.
7. **Decisions approved.** Every design or naming decision the brief depends on that meets all three ADR criteria is listed in the triage notes, with the person who approved it:
   - **Hard to reverse:** changing your mind later costs something meaningful.
   - **Surprising without context:** a future reader would wonder why it was done this way.
   - **The result of a real trade-off:** there were genuine alternatives.

   For a design decision, the recommended answer starts from the real problem and its constraints. It presents the best long-term solution first, with its cost stated up front, and offers a quick fix only as an optional first step, labelled as optional. A naming decision is exempt. An agent's recommended answer counts only once a person accepts or overrides it. An agent never approves a decision. "None needed" is a valid result.
8. **Blockers recorded.** Superseding and blocking issues were checked, and each blocker found is recorded as a native blocked-by relation, or recommended in the triage notes when the run may not write it.
9. **Owning layer.** When the scope boundaries limit files, the notes or draft name the layer that owns the rule and cite the deciding ADR or domain doc, or name the inferred layer and the reason. Scope boundaries limit files when an out-of-scope or key-interfaces entry names a path, module, package, or layer that the fix must or must not touch. The scope boundaries do not exclude the owning layer. When the owning layer differs from the layer where the symptom shows, either the scope covers the owning layer or the notes list the conflict as an open question, which fails item 5. Otherwise this item fails. When the scope boundaries limit no files, record "n/a".

## Record it

Keep the checklist in `<identifier> Triage notes`, so the next session sees what passed and what didn't:

```markdown
**Readiness check:**

- [x] Type: Tools; Theme: Deliverability & reliability
- [x] Claim verified: not built; searched the `SkillConfig` loader and the scheduler
- [x] Out of scope checked: no match
- [ ] Acceptance criteria and scope boundaries: out-of-scope list missing
- [ ] No open questions: 1 open
- [x] Reason it can't be delegated: n/a (Ready for agent)
- [ ] Decisions approved: 1 awaiting approval
- [x] Blockers recorded: blocked-by relation to PAT-123 written
- [x] Owning layer: server, per `ADR-<id>`
```

List each qualifying decision under the checklist with its answer and who approved it, or mark it awaiting approval. Record a qualifying decision only here, never under "still needed":

```markdown
**Decisions:**

- Name the label `Ready for human`, not `Needs human`: approved by @person
- Remove the `--legacy` flag instead of deprecating it: awaiting approval
```

Save `<identifier> Triage notes` and `<identifier> Agent brief` only through the issue-tracker config's **Attach an artifact** operation. `<identifier>` is the tracker's issue reference, such as `PAT-123` or `#123`. When the config defines no such operation, stop before writing anything and ask for a `setup-work-skills` re-run.

## Outcome

- **Every item holds:** write `<identifier> Agent brief` from what the checklist established, then apply the label. The brief has five core sections: Summary, Current behavior, Desired behavior, Acceptance criteria as a checklist of Given / When / Then items, and Out of scope. Add Owning layer whenever the brief limits files. Add User stories, Key interfaces, Implementation decisions, Testing decisions, or Further notes only when they carry something the agent needs. It has no Type or Theme line. When the Type needs no repository change, such as `Task`, the brief names the action to take and how to confirm it is done, not code changes.
- **An item fails:** do not apply the label. Close each gap you can yourself: reproduce the report, search the codebase, read `.out-of-scope/`. Grill only about the gaps that remain, in the grilling skill's usual format. Ask nothing the checklist already settled. Record the remaining questions under "still needed" in the triage notes, each with the person who must answer it: the reporter or a developer. Apply `Needs info` whenever a question remains, whoever must answer it.
- **"Decisions approved" fails:** follow the bullet above, but never close this gap yourself. Ask each unapproved decision as a grilling question with your recommended answer. For a design decision, that answer follows item 7. The item fails until a person answers it.
