from __future__ import annotations

import hashlib

from scripts.ci import build_pages_site
from scripts.ci.engine_matrix import DEFAULT_ENGINES


def _sample(engine: str, *, profile: str | None = None, voice: str | None = None, audio: bytes = b"sample"):
    profile_id = f"sha256:{(engine.encode().hex() * 32)[:64]}"
    audio_sha256 = hashlib.sha256(audio).hexdigest()
    binding = {"role": "narrator", "renderer": engine, "target_id": f"{engine}-target"}
    if voice is not None:
        binding["voice"] = voice
    return build_pages_site.PublishedSample(
        engine=engine,
        profile=profile or engine,
        profile_id=profile_id,
        variant=f"{engine}--{profile_id.removeprefix('sha256:')[:12]}",
        targets=({"engine_id": engine, "target_id": f"{engine}-target"},),
        bindings=(binding,),
        compound_wer=0.0,
        strict_wer=0.0,
        cer=0.0,
        word_boundary_match_count=0,
        source_platform="linux-x64",
        source_run_id="12345",
        source_attempt="1",
        audio_sha256=audio_sha256,
        audio_size_bytes=len(audio),
        audio_path=build_pages_site.content_addressed_audio_path(engine, profile_id, audio_sha256),
    )


def _manifest(samples):
    return build_pages_site.build_gallery_manifest(
        samples,
        repository="buchwandler/bookbench",
        commit="a" * 40,
        run_id="12345",
        attempt="1",
    )


def test_gallery_renders_all_samples_in_registry_order():
    samples = [_sample(engine) for engine in reversed(DEFAULT_ENGINES)]
    manifest = _manifest(samples)
    assert [sample["engine"] for sample in manifest["samples"]] == list(DEFAULT_ENGINES)

    page = build_pages_site.render_index(manifest)
    assert page.count('<audio controls preload="none"') == len(DEFAULT_ENGINES)
    for sample in manifest["samples"]:
        assert f'src="{sample["audio"]["path"]}"' in page
    assert 'preload="auto"' not in page


def test_gallery_escapes_profile_and_binding_text():
    samples = [_sample(engine) for engine in DEFAULT_ENGINES]
    samples[0] = _sample(
        DEFAULT_ENGINES[0],
        profile="Amy <script>alert(\"x\")</script> & 'voice'",
        voice="Voice < > \"quoted\" & 'single'",
    )
    page = build_pages_site.render_index(_manifest(samples))

    assert '<script>alert("x")</script>' not in page
    assert "Amy &lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; &#x27;voice&#x27;" in page
    assert "Voice &lt; &gt; &quot;quoted&quot; &amp; &#x27;single&#x27;" in page


def test_gallery_has_one_player_at_a_time_script():
    script = build_pages_site.JAVASCRIPT
    assert 'document.querySelectorAll("audio")' in script
    assert "if (audio !== event.target) audio.pause();" in script
    assert "event.target instanceof HTMLAudioElement" in script


def test_gallery_labels_metrics_as_validation_not_quality():
    page = build_pages_site.render_index(_manifest([_sample(engine) for engine in DEFAULT_ENGINES]))
    assert "spoken-content validation metrics" in page
    assert "not subjective voice-quality scores and do not rank voices" in page
    assert "best voice" not in page.casefold()


def test_gallery_uses_content_addressed_audio_paths():
    profile_id = "sha256:" + "1" * 64
    first_hash = hashlib.sha256(b"first audio").hexdigest()
    second_hash = hashlib.sha256(b"changed audio").hexdigest()
    first_path = build_pages_site.content_addressed_audio_path("piper", profile_id, first_hash)
    second_path = build_pages_site.content_addressed_audio_path("piper", profile_id, second_hash)

    assert first_path != second_path
    assert first_hash in first_path
    assert second_hash in second_path
