# pstack model configuration

Per-role model overrides for pstack skills. Each pstack SKILL.md names its defaults in a Models section; the values here override those defaults. Delete a line to fall back to the skill default. A value of `inherit-parent` or `auto` runs that role on the parent session's model (the `Agent` call omits `model`); an alias entry in a panel list still counts toward that panel's fan-out.

feature, refactoring: <model>
bug-fix: <model>
perf-issue: <model>
hillclimb: <model>
judgment and prose: <model>
strongest judgment: <model>
how explorer: <model>
how explainer: <model>
why investigators: <model>
why synthesizer: <model>
reflect tooling: <model>
reflect judgment, divergent, synthesizer: <model>
arena runners: <model>, <model>
arena cross-judge pool: <model>, <model>
swarm workers: <model>
architect runners: <model>, <model>
interrogate reviewers: <model>, <model>

default effort: <low, medium, high, xhigh, max, or session>
session hook: <on or off>
