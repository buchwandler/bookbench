# BookBench concept

## Role

BookBench is the **end-to-end acceptance and comparison harness** for the Buchwandler audiobook
pipeline. It does not own speech semantics, planning, rendering, composition, export,
or ASR. Those responsibilities stay in their existing repositories. BookBench only
executes public contracts, records evidence, and reflects each component's authoritative evaluation in run status.

## Test graph

```text
input.md
  -> text2epub
  -> ssmdconvert
  -> utterplan (shared, engine independent)
  -> renderplan variant A -> voicerender -> audiocompose -> audioexport -> decode -> speechscope
  -> renderplan variant B -> voicerender -> audiocompose -> audioexport -> decode -> speechscope
  -> ... one branch per RenderProfile
```

The expensive engine-specific work branches only after UtterPlan compilation. This
keeps the common preparation deterministic and avoids repeating EPUB/SSMD work for
every engine.

## Engine discovery

A RenderProfile is a test case. BookBench scans `profiles/*.renderprofile.toml`, reads
`roles.*.binding.renderer`, and runs every profile. `required_engines` is a coverage
gate, not an implementation list. This means new VoiceRender engines can be added
without changing BookBench code.

## Validation oracle

There are two different notions of equality:

1. **Written/source equality**: useful for structural conversion tests.
2. **Spoken equality**: the final exported audio must represent the exact speech
   requests sent to the TTS engine.

The acoustic oracle therefore defaults to the ordered `kind = "speech"` text in the
active RenderPlans. Comparing ASR directly with the original source can be wrong when
normalization or SSMD substitutions intentionally change the spoken form.

## Compound-aware spoken-content gate

BookBench's default exact spoken-content policy uses SpeechScope `max_compound_wer = 0.0` and
`max_cer = 0.0`. This rejects true lexical substitutions, deletions, and insertions while allowing
only SpeechScope's bounded adjacent compound-word split/join equivalences. Strict `max_wer` remains
an opt-in exact whitespace-token-boundary rule and is also retained as diagnostic evidence. If both
word thresholds are configured, both must pass. SpeechScope owns comparison and threshold evaluation;
BookBench uses its evaluation result and exit code rather than recomputing pass/fail. Reports preserve
compound WER, strict WER, CER, content match, and accepted boundary-variant counts separately.

For existing configs that set `max_wer = 0.0` but intend to tolerate variants such as
`audiobook` / `audio book`, migrate that key to `max_compound_wer = 0.0`; configure both only when
strict token-boundary equality is intentional.

## Why decode after export

SpeechScope v0.1 accepts WAV while the native book export is M4B. BookBench decodes the
**finished M4B** to a validation WAV and analyzes that. This tests the export step too;
validating the pre-export chapter WAVs would leave AudioExport outside the E2E gate.

## Evidence model

Every run gets an isolated directory with active tool versions, commands, timings, all
intermediate artifacts, each final export, each expected transcript, each SpeechScope
report, and one cross-engine summary. A failing engine does not erase evidence from
other engines.

## Replanning

VoiceRender capacity failures may produce valid UtterPlan replan feedback. BookBench
may retry that loop a bounded number of times. It never invents splits or renderer
logic itself. If UtterPlan refuses the replan, the engine branch fails.

## CI evolution

The MVP supports local runs and a self-hosted manual workflow. The next step is a
repository-dispatch workflow: a component repo sends `{component, ref}` to BookBench;
a self-hosted runner installs that checkout plus pinned versions of all other
components, runs the full engine matrix, and uploads the run directory as evidence.

## Comparison cases

For A/B/n tests, `[[voices]]` entries define human-facing candidates. Each candidate points to a RenderProfile. The candidate name controls report/export labels only; the RenderProfile filename and canonical `profile_id` continue to control the RenderPlan variant identity. This keeps experimental labels from mutating execution semantics.
