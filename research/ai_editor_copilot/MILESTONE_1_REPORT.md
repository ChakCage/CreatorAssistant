# Milestone 1 — real transcript narrative research

Status: EXPERIMENT_COMPLETE, 2026-09-22. Evidence foundation and evaluation complete;
narrative selection readiness NOT demonstrated. Human evaluation remains outstanding.
Base commit: `ddded852e19973d995fece479370f8e27e83c6e5`.
Branch: `feature/ai-editor-copilot-lab`. No merge/build/deploy or production changes.
Implementation/evaluation commit: `3bee3f0b7651ff45446f52a515573d4664a4c1b7`.
Research worktree was clean immediately after that commit. This report-only
provenance note is a subsequent documentation commit. Unrelated main-checkout
changes are preserved and are not part of this branch's delivery.

## Dataset and permission

The owner explicitly selected three local project directories on 2026-09-17 for
research. Source media remains read-only on E:. No media or full transcript is in Git.
Private evidence: `runs/dataset/`; entire-file SHA-256 and transcript SHA-256 in each
`input_manifest.json`. Local CA ExistingWhisperBackend, existing large-v3-turbo,
English transcription, word timestamps. No model download or toolkit installation.

| Source | FFprobe duration | Note |
|---|---:|---|
| 100 Players Simulate Minecraft's Magical Purge | 7039.779410 s (117:20) | longer than preferred 90 min; full source retained |
| I Explored Minecraft's Most Dead Server | 2945.102948 s (49:05) | original 720p, not advertising transcript |
| I Joined the World's Oldest Minecraft Server | 3864.055873 s (64:24) | original 720p |

## Implementation

See ARCHITECTURE: evidence contracts, full-coverage overlapping chunks, cached local
candidate extraction, paged global index retrieval with omission audit, exact raw
reconstruction and second evidence verification, typed refusal, graph rationale,
contiguous lexical/density baseline, mechanical metrics and blind text packages.

Raw ASR anomalies remain unmodified. Zero-duration/empty segments are retained in
context and reported, never silently deleted; zero-duration standalone clips fail.
Word timestamps are preserved even when they cross ASR segment boundaries; no claim
of word-accurate editing is made. Missing/reversed/out-of-source times are rejected.

## Evaluation protocol

10 fixed scenario probes in `evaluation/tasks.json`: seven development, three held-out.
They test scenarios rather than assert independently verified labels. Same prompt
version for all sources/tasks. Held-out loading requires explicit release flag.
Three independent planner requests per evaluated task; local chunk analysis reused.
qwen3.6:35b-a3b only, temperature 0, 32768 context, 6000 output token budget.

Human rubric covers eight 1–5 criteria plus preference/confidence/reason. No human
ratings have been supplied. Labels/method mapping stay separate from planner input.
No AI superiority claim. Task-held-out on three same-creator sources is not a
source-held-out generalization experiment. Candidate recall stays null without labels.

## Initial failures

- First STT launcher stopped when cp1251 console could not print a Unicode progress
  character. Fixed UTF-8 in research launcher only; checked no orphan process before retry.
- Whisper reports unavailable Triton kernels, uses slower word-alignment fallback.
  No CUDA Toolkit installed. Transcription subsequently completed for first sources.
- Strict first ingest reader rejected actual zero-duration/empty ASR segments.
  Reader now preserves these as explicit raw evidence anomalies, without normalizing
  their text/timing. They are not valid standalone edit ranges.
- The 2026-09-17 evaluation was interrupted before source-level results were sealed.
  Three successful chunk cache files survived; no claim is made about the missing calls.
- Initial validation rejected a WHOLE block when one local candidate exceeded 45 s.
  The resumed dev-002 run measured only 4/11 accepted blocks for dead-server and
  therefore refused nine task repetitions for incomplete coverage. Per-candidate
  quarantine now preserves valid siblings, records each rejected candidate, and
  counts a schema-valid analyzed block separately from accepted candidates.
  Seven block responses were recovered from the durable call journal without new
  LLM inference. The corrected index has 57 candidates across 11/11 blocks.
- dev-003 revealed an unbounded `reason_codes` array: Qwen repeated CONTEXT_BUDGET
  until the 6000-token output cap, leaving invalid JSON. That failed raw response is
  preserved. The research process was stopped (Ollama service left intact); schema
  v2 limits/validates reason-code count and uniqueness, bounds prose/edges, and
  explicitly omits an oversized FAILED OUTPUT from repair while retaining complete
  original evidence. The full failed output stays in the journal, not silently sliced.

## Regression testing and known unrelated failure

- Research suite, final rerun: 104 passed in 1.34 s (75 existing Milestone 0 + 29 added cases).
- Full existing application suite: 505 passed / 1 failed in 24.80 s.
  `test_full_pipeline_creates_only_expected_structure` failed at JobStore `os.replace`
  with WinError 5 on `state/video-id.json.tmp` → `state/video-id.json` in the isolated
  `m1-base` test directory. No production code was modified.
- The failed test plus semantic/transcription checks passed on isolated retry: 19/19.
  Read-only exclusive-open check after the run found the target no longer locked.
  Sysinternals handle tooling was unavailable; the process holding the transient
  handle at failure time is NOT established. No invented antivirus diagnosis.
- Compileall of research source and scripts passed. Reports/logs remain under ignored runs/.

## Evaluation results

### Resumption audit (2026-09-22)

All three sources completed local analysis in dev-004: dead-server 11/11 blocks
and 57 candidates; magical-purge 32/32 and 168; oldest-server 14/14 and 62.
All 5,505 transcript segments were covered. These are pre-dedup candidate counts,
not validated complete stories. No transcription or source extraction was repeated.
Six development tasks completed three repetitions each, with zero accepted plans.
dev-06 was interrupted before sealing its first result; its three repetitions are
restarted separately in dev-005-resume, keeping the unfinished evidence untouched.

Observed failure example: dev-03's model refusal interprets source-time separation
as montage duration, and calls the 60-second target "60.0 minutes". This is an
incorrect model rationale, not proof of duration impossibility. Other refusals
claim incomplete coverage despite complete measured transcript coverage. Retrieval
and summary-only initial decisions remain possible bottlenecks; no causal attribution
to one stage is established without a controlled follow-up experiment.

The protocol remains v2 during completion/held-out evaluation. A task-ID runner
filter changes only which fixed task is executed, not prompts or task definitions.
Research tests rerun on 2026-09-22: **103 passed in 1.10 s**.

Completed development outcome: **0/21 accepted plans**. Seventeen refusals were
model decisions (not verified absence of a coherent story); four were pipeline
validation/repair failures. End-to-end inference per repetition, excluding cached
local extraction: mean 44.708 s, range 11.672–116.047 s. No successful-plan Jaccard
or duration variance can be estimated; their values are null, not perfect stability.

| Source | Segment coverage | Source-window time coverage | Exact-span duplicates removed |
|---|---:|---:|---:|
| dead-server | 100% | 99.8811% | 0 |
| magical-purge | 100% | 99.9830% | 0 |
| oldest-server | 100% | 99.8360% | 0 |

Source-window coverage is the union of chunk boundaries, not a claim of continuous
speech. Candidate recall cannot be measured without independent event labels.
The cached runtime uses Ollama 0.34.2, Q4_K_M model digest
`07d35212591fc27746f0a317c975a6d68754fb38e9053d82e25f06057af28522`.
Observed `ollama ps`: 23 GB, 42% CPU / 58% GPU, context 32768. These figures
describe placement, not GPU utilization or a promise of reproducible wall time.

Final machine-readable measurements: `evaluation/RESULTS_SUMMARY.json`.
Final evidence roots: dev-004 (six complete tasks only), dev-005-resume (dev-06),
held-001 (three released held-out tasks). Earlier pilots and the unfinished dev-06
directory are excluded. All ten tasks have exactly three independent requests.

**30 repetitions: 0 valid AI plans, 23 model-declared refusals, 7 invalid-output
failures after bounded repair.** Held-out subset: 0/9 valid; six model refusals,
three invalid-output failures. No invalid plan was exported as executable output.
Mean inference latency: 47.257 s, minimum 11.672 s, maximum 140.125 s per repetition,
excluding cached block analysis. Successful-plan variance metrics are all null.
Mechanical baseline produced ten temporally valid selections, 50.28–59.52 seconds
for ordinary tasks and 1.66 seconds for the short-duration negative probe.
All baseline outputs have zero source jumps/overlaps; semantic quality is unmeasured.

No narrative plan succeeded, so live second-pass exact-text verification and accepted
noncontiguous reconstruction are not established by this experiment (unit tests only).
This limitation is not hidden by calling refusals successful edits.
Model-declared refusal reasons are not ground truth: in particular a model-emitted
CONTEXT_BUDGET code does not prove actual transport truncation. The application checks
and logs its own prompt byte budget separately. No superiority claim is made.

Recommendation: **NOT_READY_FOR_RESOLVE_EXECUTOR**. Improve and reevaluate narrative
selection, then conduct human review before implementing media execution.

## Baseline interpretation and next gate

The contiguous baseline is a deliberately mechanical comparator, not a semantic
oracle. It even produces a temporally valid selection for the unsupported lunar
mission request and a 1.66-second selection for the 1–2-second complete-story probe.
Those are NOT successful stories. Its role labels are positional; its structural
validity must not be reported as quality or as evidence that it beats the AI.

Before a Resolve executor: independently label a few complete episodes, review the
private blinded packages, and run a separately versioned experiment on episode-local
retrieval and explicit montage-duration units. Do not retune on the already exposed
held-out tasks and continue calling them unseen. No human comparison or actual video
montage has been performed in this milestone.
