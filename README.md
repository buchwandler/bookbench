# bookbench

`bookbench` is an end-to-end validation and comparison harness for the Buchwandler
audiobook pipeline. It owns orchestration and test evidence only; it does not duplicate the
contracts or domain logic of `ssmdconvert`, `utterplan`, `renderplan`,
`voicerender`, `audiocompose`, `audioexport`, or `speechscope`.

## Pipeline

```text
source text/Markdown
  -> text2epub
  -> ssmdconvert
  -> utterplan
  -> RenderPlan (one variant per RenderProfile)
  -> voicerender
  -> audiocompose
  -> audioexport (M4B)
  -> ffmpeg decode of the exported artifact to WAV
  -> speechscope
  -> compound WER/CER gate against exact RenderPlan speech text
```

The decode step is intentional: SpeechScope v0.1 accepts WAV, while the native
AudioExport book path produces M4B. Decoding the finished M4B means the validation
covers the export layer instead of stopping before it.

## Why the reference defaults to RenderPlan text

The literal source is not always the literal spoken form. Written text can be
normalized (`5 kg` -> a spoken form), SSMD can carry substitutions, and UtterPlan
produces the exact speech requests consumed by the renderer. BookBench reads chapter
order from `renderplan-set.toml` when available, then concatenates `kind = "speech"`
RenderPlan flow items in that order. It uses filename order only for older workspaces
without a RenderPlan set.
Set `speechscope.reference_mode = "source"` only for fixtures where written and
spoken forms are intentionally identical.

## Fast start

Requirements: Python 3.11+, FFmpeg, and the Buchwandler CLIs on `PATH`.
SpeechScope Redux also needs its optional backend dependency/model when you run the
real ASR gate. The selected VoiceRender engine/model assets must already be
available according to their own package rules.

```bash
python -m pip install -e '.[dev]'
bookbench doctor
pytest -q
bookbench run
```

The starter includes validated VoiceRender RenderProfiles for six engines. Refresh
or replace any profile with `voicerender profile create` when its renderer snapshot
or chosen voice needs updating.

The repository uses one shared `bookbench.toml` plus VoiceRender-owned
`*.renderprofile.toml` files under `profiles/`. See `profiles/README.md` for target
discovery, profile creation, and validation. Add an engine to `required_engines`
only after its real target profile has been created and validated.

Run a selected engine during onboarding, select several engines, or run a named
BookBench case:

```bash
bookbench doctor --engine piper
bookbench run --engine piper
bookbench run --engine piper --engine kitten
bookbench run --profile piper
```

With no selector, `bookbench doctor` and `bookbench run` check or execute the full
configured matrix. Common source preparation runs once, followed by the selected
profile branches. `--engine` and `--profile` are repeatable and mutually exclusive;
a misspelled or unmatched selector is an error, never a fallback to all profiles.
`bookbench compare` remains the explicit A/B/n voice-comparison workflow.

## Evidence

Each run is isolated under `.bookbench/runs/<run-id>/` and contains:

- `run.json` with versions, config/source hashes, selected chapters, selection mode and requested selectors, selected profiles/engines/variants, dry-run state, and timestamps
- `profiles.json` with only the selected candidate identities, profile hashes/paths, variants, engines, targets, and staged-profile evidence
- `versions.json` with active CLI versions and visible version-probe failures
- `engines.json` with the VoiceRender engine inventory and selected engine/target IDs for this run
- `commands.log` with each executed command's argv, separate stdout/stderr, exit status, and duration
- `source.epub`, `book.ssmdbook/`, `prepared.txt`, and generated UtterPlan, RenderPlan, VoiceRender, AudioCompose, and AudioExport artifacts
- exported `.m4b`, decoded validation `.wav`, browser listening copy, expected transcript, and SpeechScope JSON report per profile
- `summary.json` with candidate results and failure stages
- `listen.html` with selection scope, selected profiles/engines, full variants, metrics, artifacts, and per-profile failure details

## Engine matrix

BookBench discovers renderer identities from `roles.*.binding.renderer` in each
RenderProfile. `required_engines` is enforced only for the unselected full matrix;
explicit `--engine` and `--profile` runs check their selected cases without requiring
unrelated engines. The repository currently gates the validated profiles for
`inflect`, `kitten`, `kokoro`, `piper`, `pocket`, and `supertonic`. Add `sopro` only
after a pinned local or managed reference voice is available and its profile validates.

## Replanning

VoiceRender JSON is authoritative: BookBench calls `utterplan replan-book` only when the response is valid, contains failed selected items, and every failed item explicitly has `status="failed"`, `replan_required=true`, and `error_code="preflight.replan"`.

A hard reject, mixed failure set, malformed response, or incomplete failed-item record fails closed at `voicerender` without replanning. A valid capacity replan is retried only within the configured `max_replans` budget; exhausted retries preserve the last VoiceRender diagnostic.

Summaries and listening reports retain the renderer's error code/message, replan flag, preflight report, chapter, and RenderPlan ID so hard rejects are not obscured by a secondary UtterPlan failure.
## Exact vs practical ASR gates

The smoke fixture uses `max_compound_wer = 0.0` and `max_cer = 0.0`. This is an exact
spoken-content gate: true substitutions, deletions, and insertions fail, while only
SpeechScope's bounded adjacent compound-word split/join variants are tolerated and remain
visible in diagnostics. Strict WER is a diagnostic by default; set `max_wer` as well only
when exact whitespace-tokenized word boundaries are part of the test contract.

If an existing config sets `max_wer = 0.0` but intends to tolerate variants such as
`audiobook` / `audio book`, replace that key with `max_compound_wer = 0.0`. Configuring both
thresholds intentionally requires strict token-boundary equality too.

## CI direction

The included self-hosted workflow is intentionally simple. A useful next step is
to have each component repository trigger this repo with a repository-dispatch
payload containing the changed component/ref. The self-hosted runner can install
that checkout plus the pinned versions of the other components, then run the same
engine matrix and retain `.bookbench/runs/` as the evidence artifact.

## Voice comparison / A-B tests

`bookbench compare` runs an explicit `[[voices]]` matrix and generates `listen.html` with per-voice playback controls.
Copy `bookbench-voice-compare.toml.example` to `bookbench-voice-compare.toml`.
It references the included Piper, Kitten, and Kokoro RenderProfiles; change labels or
profile paths to compare other validated assets. Then run:

```bash
bookbench -c bookbench-voice-compare.toml compare
```

Open the `listen.html` path printed by BookBench. Each candidate row includes the full
execution variant, engine, compound WER, strict WER, CER, and boundary-variant count, failure stage, listening copy, and links to its M4B
export, expected transcript, and SpeechScope JSON when those artifacts are inside the run.
The HTML report is generated even if some candidates fail and preserves configured profile
order. Compound/strict WER and CER are acoustic transcript-validation measures, not perceptual voice-quality
scores or a ranking. No real synthesis runs in unit tests.
## Command split

- `bookbench run` executes every configured case with one shared source-preparation pipeline, or only the selected `--engine`/`--profile` cases.
- `bookbench doctor` validates the full configured matrix or only selected profiles and reports selection-aware engine coverage.
- `bookbench compare` requires at least two `[[voices]]` entries and remains the TOML-driven A/B/n listening workflow.

A comparison label is presentation only. The underlying UtterPlan/VoiceRender variant is always derived from the actual RenderProfile filename and canonical `profile_id`, so renaming a candidate in TOML cannot change pipeline identity.
