# FLY — Faithful Logger of Yapping

![License: MIT](https://img.shields.io/badge/license-MIT-blue)
![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue)
![macOS 14.2+](https://img.shields.io/badge/macOS-14.2+-lightgrey)
![Apple Silicon](https://img.shields.io/badge/Apple%20Silicon-only-lightgrey)
[![Sponsored](https://img.shields.io/badge/chilicorn-sponsored-brightgreen.svg?logo=data%3Aimage%2Fpng%3Bbase64%2CiVBORw0KGgoAAAANSUhEUgAAAA4AAAAPCAMAAADjyg5GAAABqlBMVEUAAAAzmTM3pEn%2FSTGhVSY4ZD43STdOXk5lSGAyhz41iz8xkz2HUCWFFhTFFRUzZDvbIB00Zzoyfj9zlHY0ZzmMfY0ydT0zjj92l3qjeR3dNSkoZp4ykEAzjT8ylUBlgj0yiT0ymECkwKjWqAyjuqcghpUykD%2BUQCKoQyAHb%2BgylkAyl0EynkEzmkA0mUA3mj86oUg7oUo8n0k%2FS%2Bw%2Fo0xBnE5BpU9Br0ZKo1ZLmFZOjEhesGljuzllqW50tH14aS14qm17mX9%2Bx4GAgUCEx02JySqOvpSXvI%2BYvp2orqmpzeGrQh%2Bsr6yssa2ttK6v0bKxMBy01bm4zLu5yry7yb29x77BzMPCxsLEzMXFxsXGx8fI3PLJ08vKysrKy8rL2s3MzczOH8LR0dHW19bX19fZ2dna2trc3Nzd3d3d3t3f39%2FgtZTg4ODi4uLj4%2BPlGxLl5eXm5ubnRzPn5%2Bfo6Ojp6enqfmzq6urr6%2Bvt7e3t7u3uDwvugwbu7u7v6Obv8fDz8%2FP09PT2igP29vb4%2BPj6y376%2Bu%2F7%2Bfv9%2Ff39%2Fv3%2BkAH%2FAwf%2FtwD%2F9wCyh1KfAAAAKXRSTlMABQ4VGykqLjVCTVNgdXuHj5Kaq62vt77ExNPX2%2Bju8vX6%2Bvr7%2FP7%2B%2FiiUMfUAAADTSURBVAjXBcFRTsIwHAfgX%2FtvOyjdYDUsRkFjTIwkPvjiOTyX9%2FAIJt7BF570BopEdHOOstHS%2BX0s439RGwnfuB5gSFOZAgDqjQOBivtGkCc7j%2B2e8XNzefWSu%2BsZUD1QfoTq0y6mZsUSvIkRoGYnHu6Yc63pDCjiSNE2kYLdCUAWVmK4zsxzO%2BQQFxNs5b479NHXopkbWX9U3PAwWAVSY%2FpZf1udQ7rfUpQ1CzurDPpwo16Ff2cMWjuFHX9qCV0Y0Ok4Jvh63IABUNnktl%2B6sgP%2BARIxSrT%2FMhLlAAAAAElFTkSuQmCC)](http://spiceprogram.org/)

**A meeting recorder for macOS that keeps your meetings on your Mac.** FLY
records your microphone **and** your computer's audio, so the other people on a
call are included, and no bot joins the meeting. Local AI models (Whisper by
OpenAI, and pyannote) turn the recording into a transcript that says *who said
what*, which you hand to the AI agent in your project folder to write the notes.
Tell people when you record; see [Privacy and consent](#privacy-and-consent).

<p align="center">
  <img src="docs/demo.gif" width="470" alt="The FLY menubar panel: start and stop a recording, process it, then name the speakers and save the transcript into a project">
</p>

```
🎙️ Record mic + computer audio  →  📝 Transcribe  →  👥 Label speakers  →  🏷️ Name them  →  🤝 Hand over to your agent
                                   Whisper large-v3  pyannote                               /fly-summarise skill
```

A short animated walkthrough is in [`docs/how-it-works.html`](docs/how-it-works.html)
(download it and open it in a browser).

## What you get

- **A menubar recorder.** Start and stop with a click; the panel shows your
  latest recordings and which ones are waiting to be saved.
- **Accurate local transcription** with Whisper `large-v3` on your Mac's GPU,
  good even in languages smaller models get wrong, such as Finnish.
- **Speaker labels**, which you turn into real names before saving.
- **Saving into projects.** Each transcript lands in a folder your agent works
  in, such as an Obsidian vault or a repo.
- **An agent skill, `/fly-summarise`,** that turns the transcript into a note.
  It checks who said what, fixes misheard names and terms, writes in the
  meeting's language, and tells you what it changed.

There is deliberately **no built-in summary**: a small local model makes things
up, while your own agent knows the project and runs a stronger model.

## Requirements

- A Mac with Apple Silicon (M1 or later) and **macOS 14.2 or later**
- **16 GB of memory** recommended
- About **5 GB of disk space**
- An internet connection while installing; afterwards FLY works offline
- An AI coding agent such as [Claude Code](https://claude.com/claude-code) to
  turn transcripts into notes (optional)

## Install

Run this in your terminal:

```bash
curl -LsSf https://raw.githubusercontent.com/yuexincreate/FLY-transcriber/main/install.sh | sh
```

It takes 5 to 20 minutes, mostly a one-time download of the speech models
(about 3 GB). It asks one question, whether to download them now or during your
first recording: press Enter for now. Leave the window open until it says
**FLY is installed**. FLY then opens, and its icon, a microphone with wings,
sits in the menubar. No accounts or tokens are needed. If the script ends with
**FLY was not fully installed**, run the same command again.

<details>
<summary>What it installs, and options</summary>

1. [uv](https://docs.astral.sh/uv/) (if you don't have it) and
   [ownscribe](https://github.com/paberr/ownscribe), the recording and
   transcription engine, with
   [MLX Whisper](https://github.com/ml-explore/mlx-examples/tree/main/whisper),
   as `uv` tools on Python 3.12.
2. The FLY app, as a `uv` tool, from the latest
   [release](https://github.com/yuexincreate/FLY-transcriber/releases).
   It brings its own `ffmpeg`.
3. The speaker model (30 MB, checksum-verified) into
   `~/.local/share/fly-transcriber/models/`.
4. The speech model (about 3 GB: Whisper `large-v3`) into
   `~/.cache/huggingface/`. It covers every language; nothing else is
   downloaded later.
5. `FLY.app` in `/Applications` (or `~/Applications`), so Spotlight finds it,
   and a login item so FLY starts with your Mac.

Options go after `sh -s --`, for example `… | sh -s -- --no-login-item`:

- `--no-login-item`: don't start FLY at login.
- `--warmup` / `--no-warmup`: download the speech models now / during your
  first recording, without asking.
- `FLY_VERSION=v0.5.1` (set before `sh`) installs that release instead of the
  latest; `FLY_VERSION=main` installs unreleased work in progress.

To read the script before running it, download [`install.sh`](install.sh) and
run `sh install.sh`.

</details>

### Allow audio access

Your first recording asks for **microphone** and **system audio** access. Allow
both; without system audio, the other people on a call aren't recorded. macOS
names the app **python3.12** in these prompts, because FLY runs on Python: that
is FLY.

## Using it

1. Click the FLY icon and **Start recording** (the dashboard has the same
   button at the top right). The icon turns red and shows a timer.
2. **Stop recording** when the meeting ends. The recording is saved and marked
   **Process**. Click it when you have a few minutes to spare (it keeps the
   Mac busy); the panel shows the progress. To process every recording as
   soon as you stop, turn on *Process recordings automatically* in Settings.
3. When it's done, open the panel and click the recording marked **Save**. Add
   a title, name each speaker (each is shown with the longest thing they said), pick a
   project, and save.
4. Ask your agent to process the inbox (`/fly-summarise` in Claude Code). It
   writes the note and, if your project allows it or you agree, deletes the raw
   transcript.

Processing takes about a minute plus an eighth of the meeting's length: a
25-minute meeting is ready about 4 minutes after you start processing it. Click a saved
recording to read its transcript. Right-click the icon for Add Project, the
recordings folder, advanced settings and Quit. If you quit FLY, open it again
from Spotlight (⌘Space, then type FLY).

There is no vocabulary to set up: the skill corrects misheard names and jargon
from what your project knows. If the meeting's language is detected wrong, pin
it in **Settings** (the sliders icon in the panel).

### Projects

A project is a folder for transcripts: a new one, or one you already have, such
as an Obsidian vault or a repo. Add it in **Settings → Projects → Add project**,
or when you save your first recording. FLY adds a `meetings/_inbox/` folder for
the transcripts, and two files for your agent: `CLAUDE.md` and the
`/fly-summarise` skill in `.claude/skills/`. It shows you everything it will
create first (its **Options** change the folder, file names and frontmatter
style), and never overwrites files that exist, so you can tailor them:
put your note template and conventions in `CLAUDE.md` and the skill follows
them. To add the agent files to any folder, run
`fly-transcriber install-agent ~/path/to/folder`.

### About speaker labels

Labels come from voice alone, so short replies and two people sharing one mic
are sometimes attributed to the wrong person. The skill checks every decision
and action item against the text before naming anyone. The names you give apply
only to the saved copy, so a wrong mapping can be redone.

## Privacy and consent

Audio, transcripts and the models stay on your Mac. Once the speech models are
downloaded, nothing is sent anywhere. (If you use a HuggingFace token instead of the bundled speaker
model, FLY checks it with HuggingFace before each recording, without sending
any audio or text.) FLY's window is a local web page that only FLY itself can
use, not other websites you visit, and your recordings and settings are
readable only by your own account.

**Tell people when you record.** In many places, including the EU, you need
participants' consent to record a meeting. It's your responsibility to ask.

## Update and uninstall

To update, run the install command again. It won't interrupt a meeting that is
being recorded or processed, and keeps your settings, recordings and downloaded
models. The version is shown at the bottom of the FLY window.

To uninstall:

```bash
curl -LsSf https://raw.githubusercontent.com/yuexincreate/FLY-transcriber/main/install.sh | sh -s -- --uninstall
```

This removes FLY, `FLY.app`, ownscribe, the speaker model and the login item.
Your settings, recordings and the speech models stay; delete
`~/.config/fly-transcriber`, `~/ownscribe` and `~/.cache/huggingface` yourself
if you want them gone.

<details>
<summary>Files and settings</summary>

| File | What it is |
|---|---|
| `~/.config/fly-transcriber/settings.toml` | App settings, also editable in Settings |
| `~/.config/fly-transcriber/hf_token` | Optional HuggingFace token, only needed without the bundled speaker model (`$HF_TOKEN` overrides it) |
| `~/.config/fly-transcriber/state.json` | What was saved where, plus the titles and names you typed |
| `~/.config/ownscribe/config.toml` | Generated from settings before each recording; don't edit it (a file FLY didn't write is backed up once, as `config.toml.bak-*`) |
| `~/ownscribe/` | Recordings and original transcripts |
| `~/.local/share/fly-transcriber/models/` | The speaker model |
| `~/.cache/huggingface/` | The speech models |

Settings worth knowing in `settings.toml`:

- `model`: `large-v3` by default. `medium` or `small` are faster but much less
  accurate outside English.
- `language`: empty to auto-detect, or a code like `"fi"` or `"en"`.
- `keep_recording`: keeps the audio file (about 300 MB per hour). Off by default.
- `engine`: `"mlx"` (default) runs Whisper on the GPU. `"faster-whisper"` uses
  the CPU, about 3× slower, and downloads a word-alignment model (0.4–4 GB) the
  first time it meets each language.

After editing the file by hand, use **Advanced → Reload Settings** in the
right-click menu.

</details>

## Known limitations

- **Apple Silicon and macOS 14.2+ only**: system audio capture relies on Core
  Audio taps.
- **Meetings are named by hand** at saving. An unnamed one is saved as
  `28-09-26-meeting-1420.md`.
- **Permission prompts say "python3.12"**, not "FLY", and there are **no macOS
  notifications**, because FLY isn't a packaged, signed Mac app yet. The menubar
  icon shows the status instead.

## Development

FLY is a thin front-end for the [ownscribe](https://github.com/paberr/ownscribe)
CLI. How it works, running it from source and releasing are in
[`DEVELOPMENT.md`](DEVELOPMENT.md).

## Credits

Built on [ownscribe](https://github.com/paberr/ownscribe),
[WhisperX](https://github.com/m-bain/whisperX),
[OpenAI Whisper](https://github.com/openai/whisper),
[MLX Whisper](https://github.com/ml-explore/mlx-examples/tree/main/whisper),
[pyannote.audio](https://github.com/pyannote/pyannote-audio) and
[PyObjC](https://github.com/ronaldoussoren/pyobjc). The UI bundles
[Preact](https://preactjs.com) (MIT) and [htm](https://github.com/developit/htm)
(Apache-2.0); their licences are in `src/fly_transcriber/static/vendor/`.

The installer downloads pyannote's
[speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1)
model, redistributed unmodified under
[CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/). Credit for it goes
to [pyannote](https://github.com/pyannote/pyannote-audio). If you use it outside
this app, please get it from the source.

## License

[MIT](LICENSE)
