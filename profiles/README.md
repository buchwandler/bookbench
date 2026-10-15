# Engine profiles

BookBench treats RenderProfiles as executable test cases. Every file matching
`*.renderprofile.toml` is run automatically.

Create profiles with VoiceRender rather than editing opaque engine bindings by hand:

```bash
voicerender voices list --engine piper --lang en-US --offline --json
voicerender profile create \
  --engine piper --target en_US-amy-medium --language en-US --offline \
  --output profiles/piper.renderprofile.toml
```

Repeat for the engines you want to gate. VoiceRender currently documents these
engine extras: `kokoro`, `piper`, `pocket`, `kitten`, `sopro`, `supertonic`, and
`inflect`. Reference-voice engines may need `--reference-wav`, `--voicefolio-ref`,
or engine options; keep those choices in the RenderProfile, not in BookBench.

Set `required_engines` in `bookbench.toml` to make missing coverage fail before any
expensive synthesis starts. Multiple profiles for the same engine are allowed and
all of them are tested.
