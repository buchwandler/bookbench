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
  -> WER/CER gate against exact RenderPlan speech text
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

The starter includes the current public Piper example profile. Replace/regenerate
it with `voicerender profile create` if its renderer snapshot becomes stale.

For all engines, add one `*.renderprofile.toml` per engine under `profiles/`, then
copy `bookbench-all-engines.toml.example` to your preferred config and run:

```bash
bookbench -c bookbench-all-engines.toml run
```

Every profile is tested, so you can also keep several voices/models for the same
engine.

## Evidence

Each run is isolated under `.bookbench/runs/<run-id>/` and contains:

- `run.json` with BookBench, Python, and platform versions, config and source paths and SHA256 values, selected chapters, dry-run state, and start/end timestamps
- `profiles.json` with candidate and source profile identities, original and staged hashes and paths, canonical variant labels, full variants, and engines
- `versions.json` with active CLI versions and visible version-probe failures
- `commands.log` with each executed command's argv, separate stdout/stderr, exit status, and duration
- `source.epub`, `book.ssmdbook/`, `prepared.txt`, and generated UtterPlan, RenderPlan, VoiceRender, AudioCompose, and AudioExport artifacts
- exported `.m4b`, decoded validation `.wav`, browser listening copy, expected transcript, and SpeechScope JSON report per profile
- `summary.json` with candidate results and failure stages
- `listen.html` with full variants, WER/CER, artifact links, and per-candidate failure details

## Engine matrix

BookBench discovers engines from `roles.*.binding.renderer` in RenderProfiles. The
`required_engines` config is a coverage gate: if a required engine has no profile,
the run fails before synthesis. VoiceRender currently documents `kokoro`, `piper`,
`pocket`, `kitten`, `sopro`, `supertonic`, and `inflect` extras.

## Replanning

VoiceRender may report a capacity failure that UtterPlan can replan. BookBench
retries the book-level render up to `max_replans`. After a failed render it asks
`utterplan replan-book` to consume the exact VoiceRender feedback. If no valid
replan is available, the engine result fails rather than guessing a repair.

## Exact vs practical ASR gates

The sample uses `max_wer = 0.0` and `max_cer = 0.0`, matching an exact smoke-test
intent. Real ASR is probabilistic and can disagree with correct audio, so a larger
book regression suite may use a small reviewed tolerance while keeping a tiny exact
fixture as the strict gate.

## CI direction

The included self-hosted workflow is intentionally simple. A useful next step is
to have each component repository trigger this repo with a repository-dispatch
payload containing the changed component/ref. The self-hosted runner can install
that checkout plus the pinned versions of the other components, then run the same
engine matrix and retain `.bookbench/runs/` as the evidence artifact.

## Voice comparison / A-B tests

`bookbench compare` runs an explicit `[[voices]]` matrix and generates `listen.html` with per-voice playback controls.
Copy `bookbench-voice-compare.toml.example` to `bookbench-voice-compare.toml` and
create the referenced RenderProfiles (the example only ships a working _profile file_
for Piper, not the other four model/profile assets). Then run:

```bash
bookbench -c bookbench-voice-compare.toml compare
```

Open the `listen.html` path printed by BookBench. Each candidate row includes the full
execution variant, engine, WER/CER, failure stage, listening copy, and links to its M4B
export, expected transcript, and SpeechScope JSON when those artifacts are inside the run.
The HTML report is generated even if some candidates fail and preserves configured profile
order. WER/CER are acoustic transcript-validation measures, not perceptual voice-quality
scores or a ranking. No real synthesis runs in unit tests.
## Command split

- `bookbench run` validates every RenderProfile discovered in `profiles_dir` (or the explicit voice cases when present).
- `bookbench compare` requires at least two `[[voices]]` entries and is the TOML-driven A/B/n listening workflow.
- `bookbench doctor` returns structured diagnostics for tool paths and versions, RenderProfile validation, canonical variants and staging targets, duplicate identities, source availability, writable work root, and required-engine coverage without synthesis.

A comparison label is presentation only. The underlying UtterPlan/VoiceRender variant is always derived from the actual RenderProfile filename and canonical `profile_id`, so renaming a candidate in TOML cannot change pipeline identity.
