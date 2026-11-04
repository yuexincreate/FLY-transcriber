# Developing FLY

```bash
git clone https://github.com/mokulawa/FLY-transcriber
cd FLY-transcriber
uv run fly-transcriber                      # run from source
uv run pytest                               # tests
uv run python scripts/dashboard_preview.py  # UI with fake data on :8757 (panel: /popover.html)
uv run --with playwright python scripts/make_demo_gif.py  # re-render docs/demo.gif (needs Chrome)
```

The UI is Preact + htm, vendored in `static/vendor/`, with no build step
and no npm. The tested logic lives in the non-UI modules; see `CLAUDE.md` for an
architecture overview.

## How it works

The app is a thin front-end for the [ownscribe](https://github.com/paberr/ownscribe)
CLI, which it runs as a subprocess. A few details are worth knowing if you work
on it:

- **Stop sends `SIGINT`.** That ends capture. Quitting during a recording asks
  first, because that really does discard it.
- **Processing waits for the user, unless `auto_process` is on.** Transcription
  and diarization tie up the Mac for minutes, so by default stopping only saves
  `recording.wav` in the meeting folder and the user presses Process later.
  ownscribe has no record-only mode, so the recording runs through
  `mlx_launch.py` with `FLY_RECORD_ONLY=1`, which swaps
  `ownscribe.pipeline._do_transcribe_and_summarize` for a no-op (and skips the
  MLX patch, so whisperx and torch aren't imported just to record). Process runs
  `ownscribe resume <folder>`, which finds the audio, transcribes it with the
  settings from the generated config, and deletes the WAV afterwards unless
  recordings are kept. A failed or cancelled run leaves the audio, so it can be
  processed again. Without ownscribe's own Python (the `uvx` fallback) there is
  no launcher, and recordings process at once as before. One run at a time:
  you can't record while processing, or the reverse.
- **Speaker turns come from ownscribe's JSON, not its markdown.** The markdown
  gives a whole segment to one speaker, which hides turn changes inside it,
  while the JSON keeps a speaker for every word. Segments are split where the
  word-level speaker changes, and single-word flips are ignored as noise.
- **The speaker model is installed locally, so no account is needed.**
  `pyannote/speaker-diarization-community-1` is gated on HuggingFace, but it's
  CC-BY-4.0 and its gate is auto-approved. The installer downloads a copy from
  this repo's releases into `~/.local/share/fly-transcriber/models/`. pyannote
  treats a model name that exists as a folder as a local model *before* it
  contacts the hub, and resolves it against the working directory, so the app
  runs ownscribe from that folder. ownscribe won't diarize without a token, so
  it gets a placeholder that is never sent anywhere. The installer pins
  ownscribe to the version this was tested with. The copy is packaged by
  `scripts/package_speaker_model.sh`.
- **Speaker labels are checked before recording.** When pyannote can't load
  its model, ownscribe still exits successfully with an unlabelled transcript.
  So without a local model, the app checks the gated model up front with a
  `HEAD` on a model file (the metadata API reports success even when access
  hasn't been granted).
- **Summaries were tried and removed.** The local model (`phi-4-mini`) invented
  a decision nobody made and drifted from Finnish into English partway through.
  Vocabulary hints went the same way: the agent writing the note knows the
  project's names and terms far better than a word list given to Whisper.
- **Whisper runs on the GPU with MLX.** ownscribe transcribes with
  faster-whisper on the CPU, which was about 80% of the wait after a meeting.
  ownscribe has no setting for another engine, so the app starts it through a
  small launcher (`mlx_launch.py`) that swaps in MLX Whisper where ownscribe
  loads its model and leaves the rest of the pipeline as it is. Each 30-second
  window is decoded on its own: feeding in the previous window's text, Whisper's
  default, made `large-v3` repeat phrases and drop sentences. Without MLX
  installed, the app runs ownscribe unchanged.
- **Word timings come from Whisper, not an alignment model.** WhisperX
  normally re-times every word with a wav2vec2 model, one per language at
  0.4–4 GB each, downloaded at the first meeting in that language: a surprise
  download, and a failed transcript if you're offline. Whisper's own timings are
  slightly rougher at speaker changes, where the first word or two of a
  sentence ("But…") can land on the previous speaker. The app gives those back
  to whoever says the rest of the sentence. With that, a 25-minute meeting had
  fewer speaker changes cutting a sentence in half (7%) than with the alignment
  model (10%), and processing was faster.
- **The UI is a local web page in a native popover.** The panel and the FLY
  window are `WKWebView`s showing the same Preact UI the app serves on
  `127.0.0.1:8756`, so it can also be opened in a browser. The popover's page is
  transparent, so the system material (Liquid Glass on macOS 26) shows through.
  The folder picker and the token prompt use `osascript`, because a native
  alert's text field can't take focus in a menubar-only app (and a web page
  can't learn a folder's path).

## Releasing

The installer installs the latest release. `main` may contain unreleased work
in progress.

To publish a release:

```bash
# 1. set `version` in pyproject.toml (e.g. 0.6.0), then refresh the lockfile
uv lock
git commit -am "Release v0.6.0" && git push
# 2. tag it and publish
git tag v0.6.0 && git push origin v0.6.0
gh release create v0.6.0 --generate-notes
```

Tests run on GitHub for every push (`.github/workflows/tests.yml`); release
from a commit where they passed. Anyone who runs the installer or updates after
that gets the new release. Changes to `install.sh` itself are live as soon as
they are on `main` (the install command fetches it from there), so keep it
working with the latest release. The speaker model has its own release,
`speaker-model-v1`, which the installer downloads separately; its name does not
start with `v`, so it is never taken for an app release.
