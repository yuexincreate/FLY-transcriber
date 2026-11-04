"""Lifecycle control for an ownscribe run.

ownscribe is driven as a subprocess rather than imported. Two reasons:

* Stopping a recording is a ``SIGINT`` -- the same thing Ctrl+C does. The signal
  ends capture and, with ``auto_process``, *starts* transcription, so stop is a
  graceful handoff, not a kill. That is far simpler across a process boundary.
  Without ``auto_process`` the run ends after capture (see ``mlx_launch.py``)
  and :meth:`Recorder.process` runs ``ownscribe resume`` on the folder later.
* The CLI is a stable contract; ownscribe's internals are not.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from . import progress
from .config import Settings, ensure_private_dir
from .diarization import MODELS_DIR, has_local_model

# Strips the cursor moves and colour codes ownscribe's live progress display emits.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\][^\x07]*\x07")
_RECORDING_RE = re.compile(r"Recording:\s*(\d+):(\d+)")


class Phase(str, Enum):
    """Where a run currently is. Ordered roughly as the pipeline proceeds."""

    IDLE = "idle"
    STARTING = "starting"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    DIARIZING = "diarizing"
    SUMMARIZING = "summarizing"
    DONE = "done"
    FAILED = "failed"


#: Phases in which the pipeline is working and must not be interrupted lightly.
PROCESSING_PHASES = {Phase.TRANSCRIBING, Phase.DIARIZING, Phase.SUMMARIZING}


@dataclass
class RunState:
    phase: Phase = Phase.IDLE
    elapsed: int = 0  # recording seconds, parsed from ownscribe's display
    detail: str = ""
    meeting_dir: Path | None = None
    error: str = ""
    started_at: float | None = None
    #: When processing began and how long it is predicted to take (seconds).
    processing_started_at: float | None = None
    processing_eta: float = 0.0
    log: list[str] = field(default_factory=list)

    @property
    def is_active(self) -> bool:
        return self.phase not in {Phase.IDLE, Phase.DONE, Phase.FAILED}

    @property
    def is_recording(self) -> bool:
        return self.phase in {Phase.STARTING, Phase.RECORDING}


def resolve_ownscribe() -> list[str]:
    """Return the command prefix used to invoke ownscribe.

    Prefers a real executable on PATH (``uv tool install ownscribe``) because it
    is a single, stable process that signals cleanly. Falls back to ``uvx``,
    which works but wraps the real process in a launcher.
    """
    found = shutil.which("ownscribe")
    if found:
        return [found]
    uvx = shutil.which("uvx")
    if uvx:
        return [uvx, "ownscribe"]
    raise FileNotFoundError(
        "ownscribe not found. Install it with: uv tool install ownscribe"
    )


#: Runs ownscribe with MLX Whisper swapped in; see that module.
MLX_LAUNCHER = Path(__file__).with_name("mlx_launch.py")


def ownscribe_python(executable: str) -> str | None:
    """The Python an installed ownscribe runs on, read from its script's shebang."""
    try:
        with open(executable, "rb") as f:
            first = f.readline(512).decode("utf-8", errors="replace").strip()
    except OSError:
        return None
    if not first.startswith("#!"):
        return None
    python = first[2:].strip()
    return python if " " not in python and os.access(python, os.X_OK) else None


def has_mlx(python: str) -> bool:
    """Whether ownscribe's environment has mlx-whisper (the installer adds it)."""
    probe = "import importlib.util, sys; sys.exit(importlib.util.find_spec('mlx_whisper') is None)"
    try:
        return subprocess.run([python, "-c", probe], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def ownscribe_command(engine: str = "mlx") -> tuple[list[str], str]:
    """The command prefix for ownscribe, and the engine it will transcribe with.

    MLX when asked for and installed; otherwise plain ownscribe (faster-whisper),
    so an install from before MLX keeps working, only slower.
    """
    base = resolve_ownscribe()
    if engine == "mlx" and len(base) == 1:
        python = ownscribe_python(base[0])
        if python and has_mlx(python):
            # -P: don't put this package's folder on the path of ownscribe's Python.
            return [python, "-P", str(MLX_LAUNCHER)], "mlx"
    return base, "faster-whisper"


#: macOS refused ScreenCaptureKit, which captures the system audio.
_CAPTURE_DENIED = ("SCStreamErrorDomain Code=-3801", "declined TCCs")


def failure_reason(output: str) -> str:
    """A readable cause for a failed run, from the end of ownscribe's output."""
    if any(marker in output for marker in _CAPTURE_DENIED):
        return (
            "macOS blocked audio capture. Allow the app that started FLY in System "
            "Settings → Privacy & Security → Screen & System Audio Recording, then "
            "restart it."
        )
    lines = [ln.strip() for ln in output.replace("\r", "\n").splitlines()]
    errors = [ln for ln in lines if ln.startswith("Error:")]
    return errors[-1].removeprefix("Error:").strip() if errors else ""


class Recorder:
    """Runs one ownscribe pipeline at a time and tracks its progress."""

    def __init__(
        self,
        settings: Settings,
        on_change: Callable[[RunState], None] | None = None,
    ) -> None:
        self._settings = settings
        self._on_change = on_change or (lambda _state: None)
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._known_dirs: set[Path] = set()
        self._engine = "faster-whisper"  # set per run by start()
        self.state = RunState()

    # -- public API ----------------------------------------------------------

    def start(self) -> None:
        """Begin recording. Raises if a run is already active.

        Unless ``auto_process`` is on, the run ends when capture does and leaves
        the audio for :meth:`process`.
        """
        with self._lock:
            if self.state.is_active:
                raise RuntimeError("A recording is already in progress")
            self.state = RunState(phase=Phase.STARTING, started_at=time.time())

        # Recordings and transcripts are for this account only.
        out_dir = ensure_private_dir(self._settings.resolved_output_dir)
        # Snapshot existing meeting dirs so the new one can be identified later;
        # ownscribe renames it to include a generated title once it has a summary.
        self._known_dirs = {p for p in out_dir.iterdir() if p.is_dir()}

        cmd, env, self._engine = self.record_command()
        self._launch(cmd, env)

    def process(self, meeting_dir: Path, audio_seconds: float = 0.0) -> None:
        """Transcribe and diarize a recording made earlier, via ``ownscribe resume``."""
        with self._lock:
            if self.state.is_active:
                raise RuntimeError("A recording is already in progress")
            prefix, self._engine = ownscribe_command(self._settings.engine)
            now = time.time()
            self.state = RunState(
                phase=Phase.TRANSCRIBING,
                elapsed=int(audio_seconds),
                detail="Transcribing",
                meeting_dir=meeting_dir,
                started_at=now,
                processing_started_at=now,
                processing_eta=progress.predict(int(audio_seconds), self._engine),
            )
        self._known_dirs = set()
        self._launch([*prefix, *self.process_args(meeting_dir)], None)

    def record_command(self) -> tuple[list[str], dict | None, str]:
        """The command for a new recording, its environment, and its engine.

        Record-only runs go through the launcher, which stops ownscribe after
        capture. Without ownscribe's own Python (the ``uvx`` fallback) there is
        no launcher, so that run processes at once, as it always did.
        """
        args = self._cli_args()
        if not self._settings.auto_process:
            base = resolve_ownscribe()
            python = ownscribe_python(base[0]) if len(base) == 1 else None
            if python:
                env = {**os.environ, "FLY_RECORD_ONLY": "1"}
                return [python, "-P", str(MLX_LAUNCHER), *args], env, "none"
        prefix, engine = ownscribe_command(self._settings.engine)
        return [*prefix, *args], None, engine

    def process_args(self, meeting_dir: Path) -> list[str]:
        """``resume`` takes the model and language as flags; the rest (diarization,
        JSON output, keep_recording) comes from the generated config."""
        s = self._settings
        # ownscribe runs from the models directory, so the folder must be absolute.
        args = ["resume", str(meeting_dir.expanduser().resolve()), "--model", s.model]
        if s.language:
            args += ["--language", s.language]
        return args

    @property
    def current_dir(self) -> Path | None:
        """The meeting folder the active run is writing, once it exists."""
        if self.state.meeting_dir is not None:
            return self.state.meeting_dir
        if self.state.is_active:
            return self._find_meeting_dir()
        return None

    def _launch(self, cmd: list[str], env: dict | None) -> None:
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            # Own process group: lets us signal the whole pipeline, including the
            # native ownscribe-audio capture helper it spawns.
            start_new_session=True,
            bufsize=0,
            # Run from the models directory so pyannote finds the local speaker
            # model by its hub name instead of downloading the gated original.
            cwd=MODELS_DIR if has_local_model() else None,
            env=env,
        )
        threading.Thread(target=self._pump_output, daemon=True).start()
        self._emit()

    def stop(self) -> None:
        """Stop recording; ownscribe saves the audio and, with ``auto_process``,
        goes on to transcribe it.

        This is the graceful path: SIGINT is exactly what Ctrl+C sends.
        """
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        if not self.state.is_recording:
            return  # already past capture; nothing to stop
        self._signal(proc, signal.SIGINT)
        self.state.detail = "Finishing capture..."
        self._emit()

    def abort(self) -> None:
        """Abandon the run entirely, discarding in-flight work."""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        self._signal(proc, signal.SIGTERM)
        # Give it a moment to unwind before insisting.
        threading.Timer(5.0, lambda: self._force_kill(proc)).start()

    # -- internals -----------------------------------------------------------

    def _cli_args(self) -> list[str]:
        """Flags that mirror settings.

        Anything without a flag (the HF token, the custom template body) is
        carried by the generated ownscribe config instead.
        """
        s = self._settings
        # The transcript is the product; notes are the receiving agent's job.
        args: list[str] = ["--no-summarize", "--model", s.model]
        if s.language:
            args += ["--language", s.language]
        args += ["--mic" if s.mic else "--no-mic"]
        args += ["--silence-timeout", str(s.silence_timeout)]
        args += ["--keep-recording" if s.keep_recording else "--no-keep-recording"]
        # Word-level speakers only exist in the JSON output.
        args += ["--format", "json"]
        if s.diarize:
            args.append("--diarize")
        return args

    @staticmethod
    def _signal(proc: subprocess.Popen, sig: int) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError):
            try:
                proc.send_signal(sig)
            except ProcessLookupError:
                pass

    @staticmethod
    def _force_kill(proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            Recorder._signal(proc, signal.SIGKILL)

    def _pump_output(self) -> None:
        """Read ownscribe's output and translate it into phase transitions."""
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        buffer = ""
        try:
            while True:
                chunk = proc.stdout.read(256)
                if not chunk:
                    break
                buffer += _ANSI_RE.sub("", chunk.decode("utf-8", errors="replace"))
                # The progress display rewrites lines without newlines, so parse
                # the rolling buffer rather than waiting for line breaks.
                self._interpret(buffer)
                buffer = buffer[-4000:]
        finally:
            self._output_tail = buffer
            self._finish(proc.wait())

    def _interpret(self, text: str) -> None:
        tail = text[-1500:]
        changed = False

        match = None
        for match in _RECORDING_RE.finditer(tail):
            pass
        if match:
            elapsed = int(match.group(1)) * 60 + int(match.group(2))
            if elapsed != self.state.elapsed:
                self.state.elapsed = elapsed
                changed = True
            if self.state.phase is Phase.STARTING:
                self.state.phase = Phase.RECORDING
                changed = True

        # Later phases win: the log is cumulative, so check in pipeline order and
        # let the furthest-along marker set the phase.
        for marker, phase in (
            ("Transcribing", Phase.TRANSCRIBING),
            ("Diarizing", Phase.DIARIZING),
            ("Identifying speakers", Phase.DIARIZING),
            ("Summarizing", Phase.SUMMARIZING),
        ):
            if marker in tail and self.state.phase is not phase:
                # Only advance forwards through the pipeline.
                if _phase_order(phase) > _phase_order(self.state.phase):
                    if self.state.processing_started_at is None:
                        self.state.processing_started_at = time.time()
                        self.state.processing_eta = progress.predict(
                            self.state.elapsed, self._engine
                        )
                    self.state.phase = phase
                    self.state.detail = marker
                    changed = True

        if "Downloading" in tail and "done" not in tail[-80:]:
            detail = "Downloading model..."
            if self.state.detail != detail:
                self.state.detail = detail
                changed = True

        if changed:
            self._emit()

    def _finish(self, returncode: int) -> None:
        if self.state.meeting_dir is None:
            self.state.meeting_dir = self._find_meeting_dir()
        if returncode == 0:
            if self.state.processing_started_at is not None:
                progress.record(
                    self.state.elapsed,
                    time.time() - self.state.processing_started_at,
                    self._engine,
                )
            self.state.phase = Phase.DONE
            # No processing markers: a record-only run, waiting to be processed.
            processed = self.state.processing_started_at is not None
            self.state.detail = "Complete" if processed else "Recording saved"
        else:
            self.state.phase = Phase.FAILED
            self.state.error = failure_reason(getattr(self, "_output_tail", "")) or (
                f"ownscribe exited with code {returncode}"
            )
            self.state.detail = self.state.error
        self._emit()

    def _find_meeting_dir(self) -> Path | None:
        """Identify the directory this run produced.

        Matched by "appeared since we started", then newest, because ownscribe
        renames the directory to include a generated title as its final step.
        """
        out_dir = self._settings.resolved_output_dir
        if not out_dir.exists():
            return None
        candidates = [
            p for p in out_dir.iterdir() if p.is_dir() and p not in self._known_dirs
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.stat().st_mtime)

    def _emit(self) -> None:
        try:
            self._on_change(self.state)
        except Exception:  # a UI callback must never take down the pump thread
            pass


_PHASE_ORDER = [
    Phase.IDLE,
    Phase.STARTING,
    Phase.RECORDING,
    Phase.TRANSCRIBING,
    Phase.DIARIZING,
    Phase.SUMMARIZING,
    Phase.DONE,
]


def _phase_order(phase: Phase) -> int:
    try:
        return _PHASE_ORDER.index(phase)
    except ValueError:
        return -1
