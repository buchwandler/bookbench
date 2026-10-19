# Engine profiles

BookBench discovers top-level `*.renderprofile.toml` files. A RenderProfile is the
single authoritative configuration for its renderer, target, voice, and engine
options. Create and validate profiles with VoiceRender rather than maintaining a
second BookBench engine configuration.

## Discover and create a profile

Use VoiceRender discovery to choose a target that is actually available in the
local environment. The human-readable list avoids assuming that every engine has
JSON-compatible discovery metadata:

```bash
voicerender voices list --engine ENGINE --lang en-US --offline
voicerender profile create \
  --engine ENGINE \
  --target TARGET_ID \
  --language en-US \
  --offline \
  --output profiles/ENGINE.renderprofile.toml
voicerender profile validate profiles/ENGINE.renderprofile.toml
```

Some targets need a discovered voice or engine option. For example, the validated
profiles use `Bella` for Kitten, `af_heart` for Kokoro, and speaker `F1` with
`seed=0` for Supertonic. Pocket no longer selects the named `alba` voice; it clones
a managed VoiceFolio reference. Sopro uses the same managed reference. Reference
voices must be pinned in `voice_source`; never put an exported local WAV path in a
committed RenderProfile.

## Profiles in this checkout

The seven validated default profiles are `inflect`, `kitten`, `kokoro`, `piper`,
`pocket`, `sopro`, and `supertonic`. Pocket and Sopro are managed-reference clones
of the same VoiceFolio entry, `kyutai-tts-voices:alba-mackenna/casual`; their
profiles pin the catalog SHA-256, source revision, repository, path, license,
dataset, and variant without storing an absolute WAV path. Sopro uses the released
`v2-turbo` target with CPU provider and `wasm-uint8` quality.

The other default profiles retain their renderer-defined baseline voice settings;
a named/predefined voice is not a cloned reference voice. VoiceRender owns profile
validation and clone semantics. VoiceFolio owns reference identity, integrity,
fetching, cache, and provenance. BookBench only runs a generic CI preparation check
and records safe evidence.

Qwen3 has dependency, runtime, lock, and model-install infrastructure, but no
profile in `profiles/`, no default-matrix entry, and no manual E2E workflow. The
reference has no authoritative exact transcript, so do not guess one or add a
named-voice fallback. Keep any later Kokoro/Inno experiment under
`profiles-experimental/` and outside default profile discovery; do not promote it
without a released-CLI-validated clone.

To check or run one engine without requiring unrelated matrix engines:

```bash
bookbench doctor --engine kitten
bookbench run --engine kitten
```

Repeat `--engine` to select a union of engines, or use `--profile LABEL` to select
an exact BookBench case label. With no selector, `bookbench doctor` and
`bookbench run` check and execute the complete configured matrix.

The `bookbench.toml` in the repository is the shared configuration. Multiple
profiles for one engine are supported and all matching cases run when selected by
engine. Use `bookbench compare` with explicit `[[voices]]` entries for A/B/n voice
comparisons.
