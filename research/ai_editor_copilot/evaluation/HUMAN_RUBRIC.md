# Blind narrative selection rubric v1

Evaluate text selections, not rendered video. No claim about image continuity, acting,
audio cuts or actual watch-time can be made. Both variants receive identical typography,
no generator, role labels, score, rationale or source timestamps. Cut boundaries and
durations are visible because they affect editability. These may still make the method
guessable; record any guess separately AFTER rating. Do not show the private mapping.

Two independent readers are preferred. Read A and B in counterbalanced random order.
Rate each criterion 1–5 independently; 2 and 4 are intermediate anchors.

| Criterion | 1 | 3 | 5 |
|---|---|---|---|
| Hook | no reason to continue | some curiosity | clear compelling question/tension |
| Comprehensibility | cannot understand without source | understandable with gaps | self-contained |
| Causal coherence | unrelated events | partly explained connections | necessary causal links clear |
| Setup → payoff | missing or unrelated ending | partial resolution | promised question meaningfully resolved |
| Information sufficiency | crucial details missing | minor inference required | all necessary context retained |
| Redundancy | many unnecessary repetitions | some removable content | no unnecessary repetition |
| Pacing (text only) | strongly uneven/dragging | adequate | purposeful concise progression |
| Overall editability | requires rewriting/inventing story | needs substantial rearrangement | plausible edit from supplied evidence |

Also record preference A/B/tie/neither, confidence 0–1, reason, evaluator pseudonym,
case ID and timestamp. NO_SELECTION is not scored as a zero-quality edit: mark refusal
appropriateness separately after reviewing the source, without changing blind ratings.
Never invent human ratings from model scores. `HumanRating` enforces range/type only.

Labels/ratings live outside the planner dataset and prompts. Store them under a separate
private `runs/human_ratings/` directory. Mapping keys go to the coordinator, not evaluator.
Report paired distributions, number of evaluators and missing ratings; do not claim
AI superiority without sufficient real independent evaluation.

The 10 tasks are pre-specified scenario PROBES, not verified labels establishing that
the source contains each scenario. Sources are only three gameplay videos from one
creator. Held-out split is TASK-held-out, not SOURCE-held-out; no generalization claim.
Task text is not changed per model failure. Freeze hashes before final held-out execution.
