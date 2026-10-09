---
name: principle-offensive-programming
description: Apply before writing an error handler, null check, fallback, optional chain over uncertain data, retry, assertion, guard branch, configuration skip, or workaround.
user-invocable: false
---

# Offensive Programming

The [line of defense](https://en.wikipedia.org/wiki/Offensive_programming)
separates untrusted external input from trusted internal state.

1. Name the exact state. Name its producer, the value or control path that
   represents it, and what the proposed handling would do. A state you cannot
   describe gets no check.
2. Classify the source. Users, files, networks, other processes, and systems
   outside the program's control are outside the line of defense. Inside means
   the program produces and maintains the state.
3. For an outside producer, validate once at the trust boundary and reject
   invalid input there. Handle expected product behavior, such as an absent
   record, permission denial, or network failure, at the boundary that owns it.
   A check at a real trust boundary is valid. Keep it.
4. For an inside producer, trace every path that can produce the state before
   classifying the condition. The trace is complete only after it accounts for
   access filtering, lifecycle transitions, asynchronous work, and partial
   projections.
5. Classify the traced internal state and select one response:

   - Handle expected, reachable product behavior at its owning boundary.
   - For a reachable invariant violation, fix the root cause at the
     authoritative writer and fail fast with identifying context. One invariant
     has one enforcing layer, so remove any downstream guard that repeats it.
     Do not recover with a fallback, default, or swallowed error.
   - For an unreachable state, write no guard. Delete or restructure the branch
     so it does not exist, and make the authoritative writer refuse the invalid
     state. Test the reachable behavior and the writer's refusal instead.

## Rules

- A catch on an unattended path ends in one of these outcomes: bounded retry,
  dead-letter storage with the full payload, or a report through an existing
  error-reporting seam with identifying context. Logging and continuing leaves
  the failure unresolved.
- Assert what must be true and crash with context when it is false. An assertion
  followed by handling of the same condition contradicts the invariant.
- Refuse a feature-flag skip, environment-variable skip, TODO workaround, or
  other shortcut over a defect. Fix the root cause. If that cause is outside the
  change's scope, tell the operator what blocks the fix. Keep the change narrow
  and leave the defect visible.
