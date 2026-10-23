#!/bin/sh
# Installer for FLY-transcriber.
#
#   curl -LsSf https://raw.githubusercontent.com/mokulawa/FLY-transcriber/main/install.sh | sh
#
# Installs uv (if missing), ownscribe and the app (which brings its own ffmpeg),
# the speaker model (no HuggingFace account needed) and the speech models, then
# registers a login item so the menubar icon starts with your Mac and adds FLY
# to Applications (/Applications when you may write there, else ~/Applications)
# so it can be opened from Spotlight after quitting.
#
# It installs the latest release (the newest v* tag), so work in progress on
# main never reaches anyone. Run it again to update.
#
# Options:
#   --no-login-item   don't start the app at login
#   --warmup          download the ~3 GB speech models now, without asking
#   --no-warmup       download them later, during the first recording, without
#                     asking (otherwise the script asks; with no keyboard to ask
#                     on, it downloads now)
#   FLY_VERSION=v0.2.0 (environment) install that release instead of the latest;
#                     FLY_VERSION=main installs work in progress
#   --uninstall       remove the app, its login item, FLY.app, models and ownscribe
#                     (settings, recordings and transcripts are kept)
set -eu

# Everything runs inside main(), called on the last line, so sh has read the
# whole script before running any of it. Piped into sh, the script is sh's
# stdin, and any command that reads stdin would otherwise swallow the rest of it
# and the install would stop silently halfway. Needs no Homebrew and no git
# (on a fresh Mac, git is a stub that asks to install the developer tools).

REPO="${FLY_REPO:-https://github.com/mokulawa/FLY-transcriber}"
LABEL="io.github.fly-transcriber"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
BIN_DIR="$HOME/.local/bin"
# ownscribe supports Python 3.12 and 3.13; uv fetches it if it isn't installed.
PYTHON="3.12"
# Pinned to the minor version this app is tested with: the local speaker model
# relies on how ownscribe and pyannote look the model up, and the MLX launcher
# on how ownscribe loads and calls Whisper.
OWNSCRIBE="ownscribe>=0.15,<0.16"
# Added to ownscribe's environment: Whisper on the GPU, about four times faster
# than ownscribe's own CPU transcription. FLY releases before v0.5.0 ignore it.
MLX_WHISPER="mlx-whisper==0.4.3"

DATA_DIR="$HOME/.local/share/fly-transcriber"
MODEL_DIR="$DATA_DIR/models/pyannote/speaker-diarization-community-1"
# The speaker model is published only on the original repo's releases, not on
# this fork's.
MODEL_URL="${FLY_MODEL_URL:-https://github.com/tuomasharkonen-ux/FLY-transcriber/releases/download/speaker-model-v1/speaker-diarization-community-1.tar.gz}"
# pyannote/speaker-diarization-community-1 (CC-BY-4.0), as packaged by
# scripts/package_speaker_model.sh.
MODEL_SHA256="5ce2bfa9a938dc132cec1172592d65173cbb8f444ea1e4133f10f9391de155be  config.yaml
7ad24338d844fb95985486eb1a464e32d229f6d7a03c9abe60f978bacf3f816e  segmentation/pytorch_model.bin
6f10ff60898a1d185fa22e1d11e0bfa8a92efec811f11bca48cb8cafebefd929  embedding/pytorch_model.bin
9b77bcd840692710dd3496f62ecfeed8d8e5f002fd991b785079b244eab7d255  plda/plda.npz
325f1ce8e48f7e55e9c8aa47e05d2766b7c48c4b25b8de8dd751e7a4cc5fbe8f  plda/xvec_transform.npz"

main() {
login_item=1
warmup=ask
action=install
for arg in "$@"; do
  case "$arg" in
    --no-login-item) login_item=0 ;;
    --warmup) warmup=1 ;;
    --no-warmup) warmup=0 ;;
    --uninstall) action=uninstall ;;
    -h|--help) echo "usage: install.sh [--no-login-item] [--warmup|--no-warmup] | --uninstall"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
# A numbered step, so people new to the terminal can see how far along it is.
step() { printf '\n\033[1m[%s/5] %s\033[0m\n' "$1" "$2"; }
# The technical detail under a step, dimmed so newcomers can skip over it.
detail() { printf '\033[2m      %s\033[0m\n' "$@"; }
fail() { printf '\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }
warn() { printf '\033[33mWarning:\033[0m %s\n' "$*" >&2; }

# Runs a command (uv, which is quiet about nothing it does not print) and shows
# on one line what it is doing right now, with a bar for the downloads. uv
# prints "Downloading torch (70.2MiB)", "Downloaded torch", "Building ..." and
# "Installed N packages" when its output is not a terminal, and this reads
# them. The full output goes to a log, shown only if the command fails. Without
# a terminal there is nothing to draw on, so it just runs.
run_with_progress() {
  rp_log="$(mktemp)"
  if [ -t 1 ]; then
    cols="$(tput cols 2>/dev/null || echo 80)"
    # The command's stdin is closed: the script itself arrives on stdin.
    { rc=0; "$@" </dev/null 2>&1 || rc=$?; echo "$rc" >"$rp_log.rc"; } \
      | tee "$rp_log" | awk -v cols="$cols" '
        function mib(s,  n, u) {
          n = s + 0; u = s; sub(/^[0-9.]+/, "", u); sub(/\).*/, "", u)
          if (u == "GiB") n *= 1024; else if (u == "KiB") n /= 1024; else if (u == "B") n /= 1048576
          return n
        }
        function draw(  b, i, filled, frac, room, line) {
          # A build is silent for a while: keep saying so until it is done.
          line = nb > 0 ? "Building " bname " (can take a minute or two)" : label
          b = ""; room = cols - 8
          if (tot > 0) {
            frac = done / tot; if (frac > 1) frac = 1
            filled = int(frac * 24 + 0.5)
            for (i = 0; i < 24; i++) b = b (i < filled ? "█" : "░")
            b = b sprintf(" %3d%%  ", frac * 100); room -= 31
          }
          line = substr(line, 1, room > 10 ? room : 10)
          printf "\r\033[K      %s%s", b, line; fflush()
        }
        BEGIN { label = "Working out what to install"; draw() }
        /^Resolved / { label = "Found " $2 " packages to install"; draw() }
        /^Downloading / && $NF ~ /^\(.*iB\)$/ { size[$2] = mib(substr($NF, 2)); tot += size[$2]; label = "Downloading " $2; draw() }
        /^ *Downloaded / { done += size[$2]; label = "Downloaded " $2; draw() }
        /^ *Building / { nb++; bname = $2; draw() }
        /^ *Built / { nb--; label = "Built " $2; draw() }
        /^Prepared / { done = tot; label = "Downloaded and built " $2 " packages"; draw() }
        /^Installed [0-9]+ packages/ { final = "Installed " $2 " packages"; label = final; draw() }
        /^ \+ / { label = "Installing " $2; draw() }
        END { printf "\r\033[K"; if (final != "") printf "\033[2m      %s\033[0m\n", final; fflush() }
      '
    rp_rc="$(cat "$rp_log.rc" 2>/dev/null || echo 1)"
    rm -f "$rp_log.rc"
  else
    rp_rc=0
    "$@" </dev/null >"$rp_log" 2>&1 || rp_rc=$?
  fi
  if [ "$rp_rc" -ne 0 ]; then
    printf '\n' >&2
    tail -n 40 "$rp_log" >&2
  fi
  rm -f "$rp_log"
  return "$rp_rc"
}

# Called on exit while installing: a non-technical user would otherwise be left
# at a prompt after some tool's output, with no idea whether it worked.
interrupted() {
  [ "$1" -eq 0 ] && return
  printf '\n\033[31mFLY was not fully installed.\033[0m The messages above say what went wrong.\n' >&2
  printf 'Running the same install command again picks up where it stopped.\n' >&2
}

# Asks whether to download the speech models now; sets warmup to 1 or 0. The
# script itself arrives on stdin, so the answer is read from the terminal.
ask_warmup() {
  if ! { true </dev/tty; } 2>/dev/null; then
    warmup=1
    return
  fi
  cat <<'EOF'
When should FLY download its speech models (about 3 GB)?

  1) Now (recommended): FLY is ready to use as soon as this finishes.
  2) Later: install the app now. The models then download during your
     first recording, so that first transcript takes longer and needs
     an internet connection.

EOF
  while :; do
    printf 'Type 1 or 2 and press Enter [1]: '
    read -r answer </dev/tty || answer=1
    case "$answer" in
      ''|1) warmup=1; return ;;
      2) warmup=0; return ;;
    esac
  done
}

# The FLY logo, a microphone with wings, as on the app icon. The wings are
# peach (256 colours: Terminal before macOS 26 has no 24-bit colour); the mic
# keeps the terminal's own text colour, so it shows on light and dark themes.
# Only on a terminal: in a log it is just noise.
logo() {
  [ -t 1 ] || return 0
  p="$(printf '\033[38;5;173m')"; r="$(printf '\033[0m')"
  echo
  printf '%s\n' \
    "${p} ▄▒▒▒▄▄${r}  ▄██▄  ${p}▄▄▒▒▒▄${r}" \
    "  ${p}▀▒▒▒▒▒▒${r}████${p}▒▒▒▒▒▒▀${r}" \
    "     ${p}▀▒▒▒${r}████${p}▒▒▒▀${r}" \
    "       █ ▀▀▀▀ █" \
    "        ▀▄▄▄▄▀" \
    "          ██" \
    "        ▀▀▀▀▀▀"
}

# True if the default speech model is already in the HuggingFace cache (a
# reinstall or update), so there is nothing big left to ask about.
speech_model_cached() {
  # The MLX weights, which v0.5.0 and later transcribe with. An update from an
  # earlier release has only faster-whisper's, and is asked like a fresh install.
  ls "$HOME"/.cache/huggingface/hub/models--mlx-community--whisper-large-v3-mlx/snapshots/*/weights.npz >/dev/null 2>&1
}

# Prints the newest v* tag (empty if it cannot be found). Sorted by number, so
# v0.10.0 beats v0.9.0. GitHub's API allows 60 requests an hour per network,
# which a shared office connection can use up, so the release GitHub marks as
# latest (a web redirect, not the API) is the fallback.
latest_release() {
  tag="$(curl -fsSL -m 20 "https://api.github.com/repos/${REPO#https://github.com/}/tags?per_page=100" 2>/dev/null \
    | sed -n 's/.*"name": *"\(v[0-9][^"]*\)".*/\1/p' \
    | sort -t. -k1.2,1n -k2,2n -k3,3n | tail -n 1)" || tag=""
  if [ -z "$tag" ]; then
    tag="$(curl -fsSLI -m 20 -o /dev/null -w '%{url_effective}' "$REPO/releases/latest" 2>/dev/null \
      | sed -n 's|.*/releases/tag/\(v[0-9][^/]*\)$|\1|p')" || tag=""
  fi
  printf '%s\n' "$tag"
}

# Checks every model file against MODEL_SHA256; $1 is the model directory.
model_ok() {
  [ -d "$1" ] && ( cd "$1" && printf '%s\n' "$MODEL_SHA256" | shasum -a 256 -c -s ) 2>/dev/null
}

# Quits a running FLY so the update (or removal) can replace it, but never one
# that is recording or still processing: that would lose a meeting.
stop_running_app() {
  state="$(curl -s -m 2 http://127.0.0.1:8756/api/state 2>/dev/null || true)"
  case "$state" in
    *'"css": "recording"'*|*'"css": "busy"'*)
      fail "FLY is recording or processing a meeting. Let it finish, then run this again." ;;
  esac
  if pgrep -f "$BIN_DIR/fly-transcriber" >/dev/null 2>&1; then
    say "Quitting the running FLY"
    pkill -f "$BIN_DIR/fly-transcriber" 2>/dev/null || true
    sleep 2
  fi
}

unload_login_item() {
  if [ -f "$PLIST" ]; then
    launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
  fi
}

if [ "$action" = uninstall ]; then
  stop_running_app
  say "Removing login item"
  unload_login_item
  if [ -x "$BIN_DIR/fly-transcriber" ]; then
    "$BIN_DIR/fly-transcriber" uninstall-launcher || true
  fi
  pkill -f "$BIN_DIR/fly-transcriber" 2>/dev/null || true
  if command -v uv >/dev/null 2>&1; then
    say "Uninstalling the app and ownscribe"
    uv tool uninstall fly-transcriber 2>/dev/null || true
    uv tool uninstall ownscribe 2>/dev/null || true
  fi
  rm -rf "$DATA_DIR"
  echo
  echo "Done. Settings in ~/.config/fly-transcriber, recordings in ~/ownscribe and"
  echo "the speech models in ~/.cache/huggingface were left in place; delete them"
  echo "yourself if you want them gone."
  exit 0
fi

# -- requirements ------------------------------------------------------------

trap 'interrupted $?' EXIT

[ "$(uname -s)" = Darwin ] || fail "macOS only."
[ "$(uname -m)" = arm64 ] || fail "Needs an Apple Silicon Mac (M1 or later)."

macos="$(sw_vers -productVersion)"
major="${macos%%.*}"
minor="$(echo "$macos" | cut -d. -f2)"
minor="${minor:-0}"
if [ "$major" -lt 14 ] || { [ "$major" -eq 14 ] && [ "$minor" -lt 2 ]; }; then
  fail "Needs macOS 14.2 or later for system audio capture (you have $macos)."
fi

free_gb="$(df -g "$HOME" | awk 'NR == 2 { print $4 }')"
if [ -n "$free_gb" ] && [ "$free_gb" -lt 6 ]; then
  warn "Only $free_gb GB of disk space is free; FLY needs about 5 GB. Free up some space if the download fails."
fi

logo
cat <<'EOF'

Installing FLY, the Faithful Logger of Yapping.

FLY records your meetings and turns them into transcripts that show who said
what. Everything happens on this Mac: FLY uses local AI models for speech
recognition, so your audio never leaves your computer and it works offline.

Those models are big, about 3 GB to download (about 5 GB of disk space in
all), and FLY needs them to make transcripts. They download once.

The whole install takes about 5 to 20 minutes, mostly the download. You can
keep using your Mac meanwhile, but leave this window open until it says
"FLY is installed".

EOF
printf '\033[2m%s\n%s\n%s\033[0m\n\n' \
  "For the technically minded: installs uv, ownscribe (WhisperX + pyannote, with" \
  "MLX Whisper) and FLY as uv tools under ~/.local, models under ~/.local/share/" \
  "fly-transcriber and ~/.cache/huggingface, FLY.app and a LaunchAgent."

if speech_model_cached; then
  warmup=1
elif [ "$warmup" = ask ]; then
  ask_warmup
fi

# -- dependencies ------------------------------------------------------------

step 1 "Installing the tools FLY runs on (uv and ownscribe)"
if command -v uv >/dev/null 2>&1; then
  detail "uv: already installed ($(command -v uv))"
else
  detail "uv: Astral's Python package manager, into ~/.local/bin"
fi
detail "$OWNSCRIBE: recording + transcription CLI (WhisperX, pyannote), with" \
  "$MLX_WHISPER, as a uv tool on Python $PYTHON (uv downloads Python if needed)"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh -s -- --quiet
  export PATH="$BIN_DIR:$PATH"
fi
run_with_progress uv tool install --python "$PYTHON" --upgrade "$OWNSCRIBE" --with "$MLX_WHISPER"

# The newest v* tag is the latest release. Tags only, so the speaker model's
# own release (speaker-model-v1) is never mistaken for one. Installed from
# GitHub's source archive rather than with git (see the top of the script).
# Work in progress on main is installed only when asked for (FLY_VERSION=main),
# never as a fallback: a lookup that fails must not hand anyone untested code.
ref="${FLY_VERSION:-}"
if [ -z "$ref" ]; then
  ref="$(latest_release)"
  [ -n "$ref" ] || fail "Could not find the latest FLY release on GitHub. Check the internet connection and run the install command again in a few minutes."
fi
stop_running_app
step 2 "Installing the FLY app ($ref)"
if [ "$ref" = main ]; then
  warn "Installing the development version (main), as FLY_VERSION asks."
  archive="$REPO/archive/refs/heads/main.tar.gz"
else
  archive="$REPO/archive/refs/tags/$ref.tar.gz"
fi
detail "fly-transcriber from $archive" \
  "as a uv tool (PyObjC menubar app; bundles ffmpeg via imageio-ffmpeg)"
run_with_progress uv tool install --python "$PYTHON" --force --reinstall-package fly-transcriber \
  "fly-transcriber @ $archive"

# -- models ------------------------------------------------------------------

step 3 "Downloading the model that tells speakers apart (30 MB)"
detail "pyannote/speaker-diarization-community-1 (CC-BY-4.0), SHA-256 checked," \
  "into ~/.local/share/fly-transcriber/models"
speaker_model=1
if model_ok "$MODEL_DIR"; then
  say "Already downloaded"
else
  tmp="$(mktemp -d)"
  extracted="$tmp/pyannote/speaker-diarization-community-1"
  if curl -fLsS "$MODEL_URL" -o "$tmp/model.tar.gz" \
      && tar -xzf "$tmp/model.tar.gz" -C "$tmp" \
      && model_ok "$extracted"; then
    rm -rf "$MODEL_DIR"
    mkdir -p "$(dirname "$MODEL_DIR")"
    mv "$extracted" "$MODEL_DIR"
  else
    speaker_model=0
    warn "Could not install the speaker model. Recording works, but speaker labels need a HuggingFace token (see below)."
  fi
  rm -rf "$tmp"
fi

if [ "$warmup" -eq 1 ]; then
  step 4 "Downloading the speech models (about 3 GB; this is the long part)"
  detail "Whisper large-v3 (MLX), for every language, from Hugging Face into" \
    "~/.cache/huggingface (fly-transcriber warmup)"
  if ! "$BIN_DIR/fly-transcriber" warmup; then
    warmup=0
    warn "The speech models did not download. FLY retries during your first recording, or run the install command again."
  fi
else
  step 4 "Skipping the speech models for now, as you chose"
fi

# -- FLY.app ------------------------------------------------------------------

step 5 "Adding FLY to your Applications and login items"
detail "FLY.app, a launcher for Spotlight, into /Applications (else ~/Applications)"
if [ "$login_item" -eq 1 ]; then
  detail "LaunchAgent ~/${PLIST#"$HOME"/}"
fi
"$BIN_DIR/fly-transcriber" install-launcher \
  || warn "Could not add FLY to Applications. The menubar app still works; start it with: fly-transcriber"

# -- login item --------------------------------------------------------------

unload_login_item
if [ "$login_item" -eq 1 ]; then
  mkdir -p "$(dirname "$PLIST")"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$BIN_DIR/fly-transcriber</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>ProcessType</key>
  <string>Interactive</string>
</dict>
</plist>
EOF
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
else
  say "Starting the app"
  nohup "$BIN_DIR/fly-transcriber" >/dev/null 2>&1 &
fi
trap - EXIT

# The menubar icon is easy to miss (or hidden behind the notch), so show the
# dashboard once the app is up; --show hands that to the running instance.
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if curl -s -m 1 -o /dev/null http://127.0.0.1:8756/api/state; then
    "$BIN_DIR/fly-transcriber" --show >/dev/null 2>&1 || true
    break
  fi
  sleep 1
done

printf '\n\033[32m\033[1mFLY is installed.\033[0m\n'
cat <<'EOF'

- FLY lives in the menubar at the top of your screen: look for a microphone
  with wings. Its window should have opened just now.
- If you quit it, open it again from Spotlight: press Cmd-Space, type FLY and
  press Enter. It also starts by itself when you log in.
- The first time you record, macOS asks to let FLY use the microphone and the
  computer's audio. The request is shown as "python3.12": that is FLY. Click
  Allow.
EOF
if [ "$warmup" -eq 0 ]; then
  cat <<'EOF'
- The speech models are not downloaded yet. They download during your first
  recording, which needs an internet connection. To download them now
  instead, run the install command again and choose 1.
EOF
fi
if [ "$speaker_model" -eq 0 ]; then
  cat <<'EOF'

To get speaker labels without the bundled model, accept the terms at
https://huggingface.co/pyannote/speaker-diarization-community-1, create a read
token at https://huggingface.co/settings/tokens, and paste it via the menubar:
right-click the FLY icon → Advanced → Set HuggingFace Token…
EOF
fi
cat <<'EOF'

You can close this window now.

To uninstall FLY later, run:
  curl -LsSf https://raw.githubusercontent.com/mokulawa/FLY-transcriber/main/install.sh | sh -s -- --uninstall
EOF
}

main "$@"
