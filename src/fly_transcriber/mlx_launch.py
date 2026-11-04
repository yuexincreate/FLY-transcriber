"""Run the ownscribe CLI with MLX Whisper in place of faster-whisper.

ownscribe transcribes with faster-whisper on the CPU, which is most of the wait
after a recording. MLX runs the same Whisper model on the Mac's GPU, about four
times faster. Capture, diarization, the output files and the log lines
``recorder.py`` reads stay ownscribe's: only the object it calls
``.transcribe(audio, ...)`` on is replaced.

Word timings come from Whisper itself, not from whisperx's wav2vec2 alignment,
which ownscribe is told to skip. Alignment needs a 0.4-4 GB model per language,
downloaded at the first meeting in that language -- a surprise download, and a
failed transcript when offline. Whisper's own timings are a little less precise
at speaker changes; ``transcript.py`` repairs the typical error.

This file runs on ownscribe's Python, not FLY's: ``recorder.ownscribe_command``
starts it with ``-P`` so FLY's own modules beside it are not importable, and it
imports nothing from FLY. It relies on these ownscribe 0.15 internals, which the
weekly upstream workflow checks: ``WhisperXTranscriber._load_model`` sets
``self._model``; ``_transcribe_inner`` calls ``self._model.transcribe(audio,
...)``, reads ``"segments"`` and ``"language"`` from the result, as from
whisperx, and aligns only if ``_should_align()``; ``_prepare_transcription_models``
takes ``load_align``.

With ``FLY_RECORD_ONLY=1`` it only records: ``ownscribe.pipeline.run_pipeline``
calls the module-level ``_do_transcribe_and_summarize`` after capture, and that
is replaced with a no-op, so the meeting folder is left holding
``recording.wav``. ``ownscribe resume <dir>`` processes it later. The MLX swap is
skipped then: it imports whisperx (and torch), which recording doesn't need.
"""

from __future__ import annotations

import os
import sys

#: Printed instead of processing; ``recorder.py`` doesn't need it, people reading the log do.
DEFERRED_MARKER = "Recording saved; processing deferred"


def repo_for(model: str) -> str:
    """The MLX conversion of a Whisper model size, e.g. ``large-v3``."""
    return f"mlx-community/whisper-{model}-mlx"


def resolve_model(repo: str, snapshot_download=None) -> str:
    """Local path of the weights, downloading them only if they aren't cached.

    The cache is tried first so that a recording never depends on the network.
    """
    if snapshot_download is None:
        from huggingface_hub import snapshot_download
    try:
        return snapshot_download(repo, local_files_only=True)
    except Exception:
        return snapshot_download(repo)


def to_whisperx(result: dict) -> dict:
    """mlx-whisper's result in the shape of an aligned whisperx result."""
    segments = []
    for s in result["segments"]:
        if not s["text"].strip():  # MLX emits empty segments over silence
            continue
        words = [
            {"word": w["word"].strip(), "start": w["start"], "end": w["end"],
             "score": w.get("probability", 0.0)}
            for w in s.get("words", [])
            if w["word"].strip()
        ]
        segments.append({"start": s["start"], "end": s["end"], "text": s["text"], "words": words})
    return {"segments": segments, "language": result["language"]}


class MlxModel:
    """Stands in for whisperx's pipeline object: same call, same result."""

    def __init__(self, path: str, language: str | None = None) -> None:
        self.path = path
        self.language = language

    def transcribe(self, audio, **_whisperx_options) -> dict:
        import mlx_whisper

        try:
            result = mlx_whisper.transcribe(
                audio,
                path_or_hf_repo=self.path,
                verbose=None,
                language=self.language,
                # Priming each 30 s window with the previous one's text made
                # large-v3 repeat phrases and drop sentences; whisperx decodes
                # windows independently too.
                condition_on_previous_text=False,
                # Diarization assigns speakers word by word.
                word_timestamps=True,
            )
        finally:
            _release()
        return to_whisperx(result)


def _release() -> None:
    """Free the weights (~3 GB) before diarization runs."""
    try:
        import importlib

        import mlx.core as mx

        holder = importlib.import_module("mlx_whisper.transcribe").ModelHolder
        holder.model = holder.model_path = None
        mx.clear_cache()
    except Exception:
        pass  # only memory is at stake


def install() -> None:
    """Make ownscribe load an ``MlxModel`` wherever it would load faster-whisper,
    and never load or run an alignment model."""
    from ownscribe.transcription.whisperx_transcriber import WhisperXTranscriber

    def _load_model(self) -> None:
        cfg = self._tx_config
        self._model = MlxModel(resolve_model(repo_for(cfg.model)), cfg.language or None)

    prepare = WhisperXTranscriber._prepare_transcription_models

    def _prepare_transcription_models(self, **kwargs) -> None:
        # warmup asks for the alignment model here without consulting _should_align.
        kwargs.update(load_align=False, show_deferred_align_note=False)
        prepare(self, **kwargs)

    WhisperXTranscriber._load_model = _load_model
    WhisperXTranscriber._should_align = lambda self: False
    WhisperXTranscriber._prepare_transcription_models = _prepare_transcription_models
    _drop_alignment_notes()


def _drop_alignment_notes() -> None:
    """Silence warmup's closing line about the alignment model.

    ownscribe prints it whatever was loaded ("not preloaded", or "ready: fi" when
    a language is set), and there is none: in the installer it is only confusing.
    """
    try:
        import click
    except ImportError:
        return
    echo = click.echo

    def _echo(message=None, *args, **kwargs):
        if isinstance(message, str) and message.startswith("Alignment model"):
            return
        echo(message, *args, **kwargs)

    click.echo = _echo


def defer_processing(pipeline=None) -> None:
    """Stop ownscribe's pipeline after capture, leaving the audio for later."""
    if pipeline is None:
        import ownscribe.pipeline as pipeline

    def _deferred(config, audio_path, out_dir, *args, **kwargs) -> None:
        print(f"{DEFERRED_MARKER}: {audio_path}", flush=True)

    pipeline._do_transcribe_and_summarize = _deferred


def main() -> None:
    if os.environ.get("FLY_RECORD_ONLY") == "1":
        defer_processing()
    else:
        install()
    from ownscribe.cli import cli

    sys.argv[0] = "ownscribe"
    sys.exit(cli())


if __name__ == "__main__":
    main()
