# Evidence cases

## Evidence case

Record these fields without inventing missing values:

- feedback source and author
- requested behavior or result
- expected and reported behavior
- exact reproduction or inspection
- required environment
- current target identity
- gathered evidence and the target it covers
- whether the host can render each local video as a clickable file link
- unavailable evidence
- known mismatches between available checks and the feedback

## Direct evidence

Evidence must inspect the behavior or result that the person reported or requested. A
proxy can add context, but it cannot decide the verdict.

- Measure time or sequence for timing and ordering feedback.
- Exercise the same public interaction for interaction feedback.
- Inspect rendered output or geometry for visual, visibility, and layout
  feedback.
- Compare returned data for data feedback.
- Inspect the named code, caller, or diff for source feedback.

A passing CI result or unit test proves only the behavior that it directly
checks. Evidence from an older commit, build, or deployment is stale.
