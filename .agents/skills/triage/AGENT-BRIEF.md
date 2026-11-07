# Writing Agent Briefs

An agent brief is an issue artifact titled `<identifier> Agent brief`, saved through the issue-tracker config's **Attach an artifact** operation when `to-spec` publishes a spec, or when triage moves an issue or PR to `Ready for agent` or `Ready for human`. `<identifier>` is the tracker's issue reference, such as `PAT-123` or `#123`. It is the authoritative specification that an AFK agent will work from. The issue description is for people, and the discussion is context: the agent brief is the contract. Triage and `to-spec` both write it from the template below.

The brief states **what the agent should do**, which stretches to both surfaces: for an issue, that's building the change from nothing; for a PR, it's what's left to do *to the existing diff*: finish it, close gaps, address review points. Same principles either way; the PR example below shows the difference.

## Principles

### Durability over precision

The issue may sit in `Ready for agent` for days or weeks. The codebase will change in the meantime. Write the brief so it stays useful even as files are renamed, moved, or refactored.

- **Do** describe interfaces, types, and behavioral contracts
- **Do** name specific types, function signatures, or config shapes that the agent should look for or modify
- **Don't** reference file paths: they go stale
- **Don't** reference line numbers
- **Don't** assume the current implementation structure will remain the same

### Behavioral, not procedural

Describe **what** the system should do, not **how** to implement it. The agent will explore the codebase fresh and make its own implementation decisions.

- **Good:** "The `SkillConfig` type should accept an optional `schedule` field of type `CronExpression`"
- **Bad:** "Open src/types/skill.ts and add a schedule field on line 42"
- **Good:** "When a user runs `/triage` with no arguments, they should see a summary of issues needing attention"
- **Bad:** "Add a switch statement in the main handler function"

### Complete acceptance criteria

The agent needs to know when it's done. Every agent brief must have concrete, testable acceptance criteria, written as a checklist of Given / When / Then items. Each criterion should be independently verifiable.

- **Good:** "Given an issue that has been through initial classification, when a maintainer runs `gh issue list --label "Needs triage"`, then the issue is listed"
- **Bad:** "Triage should work correctly"

### Explicit scope boundaries

State what is out of scope. This prevents the agent from gold-plating or making assumptions about adjacent features.

### Owning layer before scope boundaries

A brief **limits files** when an **Out of scope** or **Key interfaces** entry names a path, module, package, or layer that the fix must or must not touch. An entry that excludes only behavior or features does not limit files.

Before a brief limits files, find the layer that owns the rule the fix changes, such as the server, the client, or a shared package. Read the repository's ADRs and the entry points its domain doc lists. The domain doc is the `docs/agents/domain.md` that `setup-work-skills` seeds, or the equivalent the repository names. Write the layer in the **Owning layer** section and cite the decision by ADR identifier or path. When no decision settles it, name the layer you inferred and the reason.

When a decision gives the rule to a different layer than the one where the symptom shows, exactly two results are allowed:

- Scope the fix to the owning layer, and keep that layer out of **Out of scope**.
- Record the conflict in the triage notes as an open question for a person. This fails the readiness check.

## Template

Write the core sections every time. Add an optional section only when it carries something the agent needs.

```markdown
## <identifier> Agent brief

**Summary:** one-line description of what needs to happen

**Current behavior:**
Describe what happens now. For a report of something broken, this is the broken behavior.
Otherwise, this is the status quo the change builds on.

**Desired behavior:**
Describe what should happen after the agent's work is complete.
Be specific about edge cases and error conditions.

**User stories:** (optional)
1. As an <actor>, I want a <feature>, so that <benefit>

**Owning layer:** (required when the brief limits files)
The layer that owns the rule the fix changes, citing the deciding ADR or
domain doc. With no deciding record, the inferred layer and the reason.

**Key interfaces:** (optional)
- `TypeName`: what needs to change and why
- `functionName()` return type: what it currently returns vs what it should return
- Config shape: any new configuration options needed

**Implementation decisions:** (optional)
- Decisions already made: modules, interfaces, schema changes, API contracts

**Testing decisions:** (optional)
- What to test, through which seams, and prior art for the tests

**Acceptance criteria:**
- [ ] Given <state>, when <action>, then <observable result>
- [ ] Given <state>, when <action>, then <observable result>

**Out of scope:**
- Thing that should NOT be changed or addressed in this issue
- Adjacent feature that might seem related but is separate

**Further notes:** (optional)
Anything else the agent needs.
```

## Examples

### Good agent brief (Bug)

```markdown
## PAT-123 Agent brief

**Summary:** Skill description truncation drops mid-word, producing broken output

**Current behavior:**
When a skill description exceeds 1024 characters, it is truncated at exactly
1024 characters regardless of word boundaries. This produces descriptions
that end mid-word (e.g. "Use when the user wants to confi").

**Desired behavior:**
Truncation should break at the last word boundary before 1024 characters
and append "..." to indicate truncation.

**Owning layer:**
The shared metadata package owns description processing, per `ADR-<id>`.
The client only displays the stored value. The truncation shows in the
client, but the fix belongs in the shared package.

**Key interfaces:**
- The `SkillMetadata` type's `description` field: no type change needed,
  but the validation/processing logic that populates it needs to respect
  word boundaries
- Any function that reads SKILL.md frontmatter and extracts the description

**Acceptance criteria:**
- [ ] Given a description under 1024 chars, when it is processed, then it is
      unchanged
- [ ] Given a description over 1024 chars, when it is processed, then it is
      truncated at the last word boundary before 1024 chars and ends with "..."
- [ ] Given any truncated description, when its length is measured, then the
      total including "..." does not exceed 1024 chars

**Out of scope:**
- Changing the 1024 char limit itself
- Multi-line description support
- Display-side truncation in the client
```

### Good agent brief (Tools)

```markdown
## #42 Agent brief

**Summary:** Add `.out-of-scope/` directory support for tracking rejected feature requests

**Current behavior:**
When a feature request is rejected, the issue is closed with a `Won't fix` label
and a comment. There is no persistent record of the decision or reasoning.
Future similar requests require the maintainer to recall or search for the
prior discussion.

**Desired behavior:**
Rejected feature requests should be documented in `.out-of-scope/<concept>.md`
files that capture the decision, reasoning, and links to all issues that
requested the feature. When triaging new issues, these files should be
checked for matches.

**Key interfaces:**
- Markdown file format in `.out-of-scope/`: each file should have a
  `# Concept Name` heading, a `**Decision:**` line, a `**Reason:**` line,
  and a `**Prior requests:**` list with issue links
- The triage workflow should read all `.out-of-scope/*.md` files early
  and match incoming issues against them by concept similarity

**Acceptance criteria:**
- [ ] Given a rejected request, when it is closed as `Won't fix`, then a file
      in `.out-of-scope/` holds the decision, reasoning, and a link to the
      closed issue
- [ ] Given a matching `.out-of-scope/` file, when another request is closed
      for the same concept, then the new issue is appended to its "Prior
      requests" list and no second file is created
- [ ] Given a new issue that matches a prior rejection, when it is triaged,
      then triage surfaces the matching `.out-of-scope/` file

**Out of scope:**
- Automated matching (human confirms the match)
- Reopening previously rejected features
- Rejected reports of something broken (every other rejection goes to `.out-of-scope/`)
```

### Good agent brief (PR)

For a PR, "Current behavior" describes the state of the diff, and the brief asks the agent to finish or fix it rather than build from scratch.

```markdown
## #57 Agent brief

**Summary:** Finish the contributor's `--json` output flag for `triage list`

**Current behavior:**
The PR adds a `--json` flag that serializes the issue list to JSON. The happy
path works and the diff matches the project's command structure. Two gaps
remain: errors are still printed as human text (not JSON), and the new flag has
no test coverage.

**Desired behavior:**
With `--json`, all output (including errors) is well-formed JSON on stdout,
and the command's exit codes are unchanged. The existing human-readable output
is untouched when the flag is absent.

**Key interfaces:**
- The command's error path should emit `{ "error": string }` under `--json`
  instead of the plain-text error
- Reuse the existing serializer the PR already added; don't introduce a second

**Acceptance criteria:**
- [ ] Given a success or an error, when `triage list --json` runs, then it
      emits valid JSON
- [ ] Given any input, when the command runs with and without `--json`, then
      the exit codes match
- [ ] Given the test suite, when it runs, then a test covers the `--json`
      success output and one error case
- [ ] Given no `--json` flag, when the command runs, then its output is
      byte-for-byte unchanged

**Out of scope:**
- Adding `--json` to any other command
- Changing the JSON shape of the success payload the PR already defined
```

### Bad agent brief

```markdown
## #61 Agent brief

**Summary:** Fix the triage bug

**What to do:**
The triage thing is broken. Look at the main file and fix it.
The function around line 150 has the issue.

**Files to change:**
- src/triage/handler.ts (line 150)
- src/types.ts (line 42)
```

This is bad because:
- Vague description ("the triage thing is broken")
- References file paths and line numbers that will go stale
- No acceptance criteria
- No scope boundaries
- No description of current vs desired behavior
