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
profiles in this checkout use `Bella` for Kitten, `af_heart` for Kokoro, `alba`
for Pocket, and speaker `F1` with `seed=0` for Supertonic. Keep those values in
the RenderProfile. Reference-voice engines may also require a real pinned local
or managed reference input.

## Profiles in this checkout

The current matrix includes validated profiles for `inflect`, `kitten`, `kokoro`,
`piper`, `pocket`, and `supertonic`. `sopro` is not gated because profile creation
requires a pinned local or managed reference voice that is not available here.
Do not add guessed targets or placeholder reference paths. Add the engine to
`required_engines` only after its profile can be created and validated.

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
