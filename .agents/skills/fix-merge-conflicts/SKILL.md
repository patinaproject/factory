---
name: fix-merge-conflicts
description: Resolve in-progress merge or rebase conflicts, verify the result, and complete the operation
---

# Fix merge conflicts

## Trigger

Branch has unresolved merge conflicts and needs a reliable path to a buildable state.

## Workflow

1. Inspect the merge or rebase state, its history, and every conflicting file.
2. Trace each side to its primary sources: commits, pull requests, issues, and the code that establishes the intended behavior.
3. Resolve each conflict with minimal, correctness-first edits. Preserve both intents when they are compatible. Otherwise, follow the operation's goal and record the trade-off. Do not invent new behavior.
4. Regenerate lockfiles with package manager tools instead of hand-editing.
5. Run compile, lint, and relevant tests.
6. Stage every resolved file and verify that no conflict marker remains.
7. Finish the merge or continue the rebase until the operation is complete. Resolve and verify each additional conflict before continuing.

## Guardrails

- Keep changes minimal and readable.
- Do not leave conflict markers in any file.
- Avoid broad refactors while resolving conflicts.
- Complete the current operation instead of aborting it.
- Do not push or tag during conflict resolution.

## Output

- Files resolved
- Notable resolution choices
- Build/test outcome
- Completed merge or rebase state
