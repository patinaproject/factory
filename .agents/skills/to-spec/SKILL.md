---
name: to-spec
description: "Turn the current conversation into a spec and publish it to the project issue tracker, applying a ready label only when the triage readiness check passes."
disable-model-invocation: true
---

This skill takes the current conversation context and codebase understanding and produces a spec. Do NOT interview the user; just synthesize what you already know. The one exception is a decision the readiness check needs a person to approve. Ask each one as a grilling question with your recommended answer.

The issue tracker and triage label vocabulary should have been provided to you. If not, tell the user to run `/setup-work-skills`.

## Process

1. Explore the repo to understand the current state of the codebase, if you haven't already. Use the project's domain glossary vocabulary throughout the spec, and respect any ADRs in the area you're touching. Before the spec limits files, name the layer that owns the rule and cite the deciding ADR or domain doc, as the **Owning layer before scope boundaries** principle in [../triage/AGENT-BRIEF.md](../triage/AGENT-BRIEF.md) describes.

2. Sketch out the seams at which you're going to test the feature. Existing seams should be preferred to new ones. Use the highest seam possible. If new seams are needed, choose them at the highest point you can. The fewer seams across the codebase, the better - the ideal number is one.

Choose the seams with your own judgment and continue without asking the user. Record them in the brief's Testing decisions, with the reason each new seam is needed.

3. Write the spec as an agent brief, using the template in [../triage/AGENT-BRIEF.md](../triage/AGENT-BRIEF.md). Fill its User stories, Implementation decisions, and Testing decisions as well as its core sections. Make User stories a long, numbered list that covers every aspect of the feature. Implementation decisions name the modules and interfaces to change, schema changes, API contracts, and architectural decisions. Testing decisions say what makes a good test here (external behavior, not implementation details), which modules are tested, the seams, and prior art in the codebase. Leave out file paths and code snippets, except a prototype snippet that encodes a decision more precisely than prose can. Trim that snippet to the decision-rich parts and note that it came from a prototype.

4. Run the triage readiness check in [../triage/READINESS.md](../triage/READINESS.md) against the spec. Its Type item holds once you have chosen the Type. Before you publish, close the gaps you can and ask about unapproved decisions, as its **Outcome** section says.

5. Confirm the issue-tracker config defines the **Attach an artifact** operation. When it does not, stop before writing anything and tell the user to re-run `/setup-work-skills`.

6. Publish the issue description for people: a short summary of the problem and the solution, from the user's perspective. On an existing issue, keep its original text first and unchanged, and add the new context below a `---` line.

7. Save the full spec as `<identifier> Agent brief` through the **Attach an artifact** operation, where `<identifier>` is the tracker's issue reference, such as `PAT-123` or `#123`. Then follow READINESS.md's **Record it** and **Outcome** sections: record the checklist and decisions, and apply the Triage label the outcome names. Apply one Type label and `Ready for agent` only after every readiness item holds. With a ready label, also apply the ready state from the issue-tracker config's **When an issue becomes ready** section.

8. End the run with a short report and nothing else that asks for work. Name the issue identifier and its link, or its path when the tracker has no link, the readiness outcome, and the labels applied. With a ready label, say the issue is ready for an implementation agent. Without one, name each open question and say it is recorded in `<identifier> Triage notes`. Do not offer, suggest, or ask to implement the issue, in prose, a question, a next-step item, a suggested action, or button text. When the operator asks for the implementation in a later message, treat it as a new request.
