"""FLY-transcriber: the macOS menubar app."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from . import config as appconfig
from .config import Settings, apply_ownscribe_config, load_settings, save_settings
from .diarization import check_diarization
from .dialogs import ask_text, choose_folder
from .filing import file_meeting
from .launcher import install_launcher, remove_launcher
from .meetings import audio_duration, list_meetings, move_to_trash, parse_meeting_dir
from .projects import apply_plan, display_path, install_agent_files, plan_from_request, plan_to_dict
from .recorder import Phase, RunState, Recorder, ownscribe_command
from .server import APP_ID, Api, serve_in_background, show_running_instance
from .shell import Shell, activate, alert, call_on_main
from .transcript import merge_speakers, resolve_merges, speakers
from .transcript import samples as transcript_samples
from .transcript import render as render_turns
from . import progress
from . import state as meeting_state

RECENT_LIMIT = 8


def _duration(meeting) -> float:
    return meeting.duration


def _processing_progress(run: RunState) -> float | None:
    if run.processing_started_at is None:
        return None
    return progress.fraction(time.time() - run.processing_started_at, run.processing_eta)


def app_version() -> str:
    try:
        return version("fly-transcriber")
    except PackageNotFoundError:  # running from a checkout that was never installed
        return "dev"


#: Matches install.sh's REPO; raw.githubusercontent.com lets install.sh run
#: without git (a fresh Mac's stub prompts to install developer tools).
UPDATE_REPO = "yuexincreate/FLY-transcriber"
UPDATE_INSTALL_URL = f"https://raw.githubusercontent.com/{UPDATE_REPO}/main/install.sh"
UPDATE_LOG = Path("~/.local/share/fly-transcriber/update.log").expanduser()
#: Shown when an update from the app fails: the same update, run where it can be watched.
UPDATE_COMMAND = f"curl -LsSf {UPDATE_INSTALL_URL} | sh"
_ESCAPES_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def installer_error(output: str) -> str:
    """The reason install.sh gave for stopping, from its log output.

    Its errors are lines starting "Error:" (after colour codes are removed);
    the last one is the one that stopped it.
    """
    lines = [_ESCAPES_RE.sub("", ln).strip() for ln in output.replace("\r", "\n").splitlines()]
    errors = [ln.removeprefix("Error:").strip() for ln in lines if ln.startswith("Error:")]
    if errors:
        return errors[-1]
    tail = [ln for ln in lines if ln]
    return tail[-1] if tail else "The installer stopped without saying why."


def _version_tuple(text: str) -> tuple[int, ...]:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", text.strip())
    return tuple(int(g) for g in match.groups()) if match else ()


def latest_release_tag(timeout: float = 10.0) -> str | None:
    """The newest ``v*`` release tag on GitHub, or ``None`` if it can't be found.

    Mirrors install.sh's ``latest_release``: tags first (sorted by number, so
    v0.10.0 beats v0.9.0, and the speaker model's own ``speaker-model-v1`` tag
    is never mistaken for a release), then the release GitHub marks as latest,
    in case the tags lookup used up the shared rate limit.
    """
    try:
        request = urllib.request.Request(
            f"https://api.github.com/repos/{UPDATE_REPO}/tags?per_page=100",
            headers={"Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            tags = json.load(response)
        versions = [(_version_tuple(t["name"]), t["name"]) for t in tags]
        versions = [v for v in versions if v[0]]
        if versions:
            return max(versions)[1]
    except (OSError, ValueError, TypeError, KeyError):
        pass
    try:
        request = urllib.request.Request(
            f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest",
            headers={"Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            tag = json.load(response).get("tag_name")
        return tag if _version_tuple(tag or "") else None
    except (OSError, ValueError, TypeError):
        return None


def _fmt_duration(seconds: int) -> str:
    return f"{seconds // 60:d}:{seconds % 60:02d}"


class MeetingRecorderApp:
    def __init__(self) -> None:
        self.settings: Settings = load_settings()
        self.recorder = Recorder(self.settings, on_change=self._on_recorder_change)
        self._dirty = threading.Event()
        self._last_phase: Phase = Phase.IDLE
        self._needs_refresh = False
        #: Recordings with a transcript that are neither filed nor skipped.
        self._awaiting = 0
        #: The installer started by an update, and where its output begins in the log.
        self._update_proc: subprocess.Popen | None = None
        self._update_offset = 0
        #: Recordings waiting for the user to process them.
        self._unprocessed = 0

        self._server, self.dashboard_url = self._start_server()
        self.shell = Shell(
            self.dashboard_url, menu=self._menu, confirm_quit=self._confirm_quit,
            on_reopen=self._show_dashboard,
        )

        self._refresh_awaiting()
        self._render(self.recorder.state)
        # The recorder and the web server run on background threads; all UI
        # mutation happens here, on the main thread, driven by this timer.
        self.shell.every(1.0, self._tick)

    def run(self, show: bool = False) -> None:
        if show:
            # Opened by hand (Spotlight, Finder): the menubar icon can be hidden
            # behind the notch or other icons, so show something that cannot be.
            call_on_main(self._show_dashboard)
        self.shell.run()

    def _show_dashboard(self) -> None:
        self.shell.open_window()

    # -- right-click menu ----------------------------------------------------

    def _menu(self) -> list:
        """The secondary menu. Everyday use goes through the popover."""
        recording = self.recorder.state.is_recording
        return [
            ("Stop Recording" if recording else "Start Recording", self.toggle_recording),
            None,
            ("Open FLY", lambda _: self.shell.open_window("#/")),
            ("Settings…", lambda _: self.shell.open_window("#/settings")),
            ("Add Project…", lambda _: self.shell.open_window("#/settings/add-project")),
            ("Open Recordings Folder", self.open_recordings),
            None,
            ("Advanced", [
                ("Set HuggingFace Token…", self.set_token),
                ("Apply Configuration", self.apply_config),
                ("Reload Settings", self.reload_settings),
                ("Open Settings File", self.open_settings_file),
            ]),
            None,
            ("Quit FLY", self.quit_app),
        ]

    def _refresh_awaiting(self) -> None:
        meetings = list_meetings(self.settings.resolved_output_dir)
        self._awaiting = len(meeting_state.awaiting_filing(meetings))
        # The folder being recorded into or processed isn't waiting.
        current = self.recorder.current_dir if self.recorder.state.is_active else None
        self._unprocessed = sum(
            1 for m in meetings if m.needs_processing and m.directory != current
        )

    # -- recording -----------------------------------------------------------

    def toggle_recording(self, _sender=None) -> None:
        if self.recorder.state.is_recording:
            self.stop_recording()
        else:
            self.start_recording()

    def stop_recording(self) -> None:
        # SIGINT ends capture (and, with auto_process, starts processing).
        self.recorder.stop()
        self._dirty.set()

    def process_meeting(self, directory: Path) -> None:
        """Transcribe a recording that was saved without processing."""
        self.shell.close_popover()
        if self.recorder.state.is_active:
            busy = "recording" if self.recorder.state.is_recording else "processing"
            alert(
                "FLY is busy",
                f"Another recording is {busy}. Process this one when it has finished.",
            )
            return
        if not self._prepare_run(action="Process"):
            return
        meeting = parse_meeting_dir(directory)
        audio = meeting.audio_path
        try:
            self.recorder.process(directory, audio_duration(audio) if audio else 0.0)
        except Exception as exc:
            alert("Could not start processing", str(exc))
            return
        self._dirty.set()

    def _prepare_run(self, action: str) -> bool:
        """Write the ownscribe config and confirm speaker labels can be made.

        Returns False when the user cancels or the config can't be written.
        """
        # Regenerate the ownscribe config so the token, template and model in
        # effect always match the current settings.
        try:
            apply_ownscribe_config(self.settings)
        except Exception as exc:
            alert("Configuration failed", str(exc))
            return False

        # Diarization failures are silent -- ownscribe exits 0 with an unlabelled
        # transcript -- so verify access before committing to a run.
        if self.settings.diarize:
            access = check_diarization(appconfig.read_hf_token())
            if not access.ok:
                return alert(
                    "Speaker labels unavailable",
                    f"{access.reason}\n\nThe transcript will have no speaker "
                    f"labels. {action} anyway?",
                    ok=action,
                    cancel="Cancel",
                )
        return True

    def start_recording(self) -> None:
        state = self.recorder.state
        if state.is_recording:
            return
        # The popover closes on its own once an alert takes focus; closing it
        # first keeps it from flickering behind one.
        self.shell.close_popover()
        if state.is_active:
            alert(
                "Still processing",
                "Transcription is still running. Wait for it to finish before "
                "starting a new recording.",
            )
            return

        if not self._prepare_run(action="Record"):
            return

        try:
            self.recorder.start()
        except Exception as exc:
            alert("Could not start recording", str(exc))
            return
        self._dirty.set()

    def _on_recorder_change(self, _state: RunState) -> None:
        # Called from the reader thread: only flag, never touch the UI here.
        self._dirty.set()

    def _tick(self) -> None:
        state = self.recorder.state
        if state.is_recording or self._dirty.is_set():
            self._dirty.clear()
            self._render(state)
        if state.phase is not self._last_phase:
            self._on_phase_change(state)
            self._last_phase = state.phase
        if self._needs_refresh:
            # Raised by the web UI thread; the icon may only be touched here.
            self._needs_refresh = False
            self._refresh_awaiting()
            self._render(state)

    def _render(self, state: RunState) -> None:
        """The menubar icon: a timer while recording, a count of unfiled ones."""
        if state.is_recording:
            elapsed = _fmt_duration(state.elapsed)
            self.shell.set_status("recording", elapsed, f"Recording — {elapsed}")
        elif state.is_active:
            self.shell.set_status("busy", "", state.detail or "Processing")
        elif state.phase is Phase.FAILED:
            self.shell.set_status("failed", "", f"Failed: {state.error}")
        elif self._awaiting or self._unprocessed:
            parts = [
                f"{n} to {verb}"
                for n, verb in ((self._unprocessed, "process"), (self._awaiting, "save"))
                if n
            ]
            n = self._awaiting + self._unprocessed
            self.shell.set_status("idle", str(n), f"Recordings: {', '.join(parts)}")
        else:
            self.shell.set_status("idle", "", "FLY")

    def _on_phase_change(self, state: RunState) -> None:
        # Also catches ownscribe stopping itself on the silence timeout.
        if state.phase in (Phase.DONE, Phase.FAILED):
            self._refresh_awaiting()
            self._render(state)
        if state.phase is Phase.DONE:
            self._warn_if_speakers_missing(state)

    # -- web UI backend ------------------------------------------------------

    def _start_server(self):
        api = Api(
            snapshot=self._snapshot,
            file_meeting=self._api_file,
            save_settings=self._api_settings,
            forget=self._api_forget,
            meeting_detail=self._api_meeting_detail,
            reveal=self._api_reveal,
            dismiss=self._api_dismiss,
            record=self._api_record,
            process=self._api_process,
            show=self._api_show,
            delete=self._api_delete,
            plan_project=self._api_plan_project,
            add_project=self._api_add_project,
            remove_project=self._api_remove_project,
            reveal_project=self._api_reveal_project,
            choose_folder=self._api_choose_folder,
            check_update=self._api_check_update,
            apply_update=self._api_apply_update,
            rename_speaker=self._api_rename_speaker,
        )
        try:
            return serve_in_background(api)
        except OSError as exc:
            # A stale instance holding the port must not stop the recorder.
            alert("FLY is already running?", f"Could not start the UI server: {exc}")
            return None, ""

    def _snapshot(self) -> dict:
        """Everything the dashboard renders. Called from the server thread."""
        run = self.recorder.state
        current = self.recorder.current_dir if run.is_active else None
        ledger = meeting_state.load_all()
        meetings = []
        for meeting in list_meetings(self.settings.resolved_output_dir, RECENT_LIMIT):
            entry = ledger.get(meeting.directory.name, meeting_state.MeetingState())
            turns = meeting.turns  # parsed once; it is re-read from disk per access
            processing = current is not None and current == meeting.directory
            meetings.append(
                {
                    "name": meeting.directory.name,
                    "title": entry.title or meeting.title,
                    "when": meeting.started.strftime("%d.%m. %H:%M") if meeting.started else "",
                    "duration": _duration(meeting),
                    "has_transcript": meeting.has_transcript,
                    "has_audio": meeting.audio_path is not None,
                    # A recording that is still being captured isn't waiting yet.
                    "needs_processing": meeting.needs_processing and not processing,
                    "processing": processing,
                    # Effective speakers (after merges) drive the views; the raw
                    # ones and their samples are what the save form merges from.
                    "speakers": speakers(merge_speakers(turns, entry.speaker_merges)),
                    "raw_speakers": speakers(turns),
                    "samples": transcript_samples(turns),
                    "filed": [
                        {"project": f.project, "path": f.path, "at": f.at}
                        for f in entry.filed
                    ],
                    "state": {
                        "title": entry.title,
                        "speaker_names": entry.speaker_names,
                        "speaker_merges": entry.speaker_merges,
                        "pending_project": entry.pending_project,
                        "dismissed": entry.dismissed,
                    },
                }
            )
        return {
            "version": app_version(),
            "update": self._update_status(),
            "run": self._run_summary(run),
            "meetings": meetings,
            "projects": [
                {
                    "name": p.name,
                    "path": str(p.resolved_path),
                    "display": display_path(p.resolved_path),
                    "naming": p.naming,
                    "frontmatter": p.frontmatter,
                }
                for p in self.settings.projects
            ],
            "settings": {
                "model": self.settings.model,
                "language": self.settings.language,
                "silence_timeout": self.settings.silence_timeout,
                "speaker_count": self.settings.min_speakers,
                "mic": self.settings.mic,
                "diarize": self.settings.diarize,
                "keep_recording": self.settings.keep_recording,
                "auto_process": self.settings.auto_process,
            },
        }

    @staticmethod
    def _run_summary(run: RunState) -> dict:
        elapsed = _fmt_duration(run.elapsed)
        if run.is_recording:
            label = "Recording" if run.phase is Phase.RECORDING else "Starting…"
            return {"css": "recording", "label": f"{label} — {elapsed}", "detail": "", "elapsed": elapsed}
        if run.is_active:
            return {
                "css": "busy",
                "label": run.detail or run.phase.value.title(),
                "detail": f"{elapsed} captured",
                "elapsed": elapsed,
                "progress": _processing_progress(run),
            }
        if run.phase is Phase.FAILED:
            return {"css": "failed", "label": "Failed", "detail": run.error, "elapsed": ""}
        return {"css": "idle", "label": "Idle", "detail": "", "elapsed": ""}

    def _api_file(self, name: str, project_name: str, payload: dict) -> dict:
        project = self.settings.project(project_name)
        if project is None:
            raise ValueError(f"Unknown project {project_name!r}")
        directory = self._meeting_directory(name)

        title = (payload.get("title") or "").strip()
        names = {
            str(k): str(v).strip()
            for k, v in (payload.get("speaker_names") or {}).items()
            if str(v).strip()
        }
        meeting = parse_meeting_dir(directory)
        known = set(speakers(meeting.turns))
        merges = resolve_merges({
            str(k): str(v)
            for k, v in (payload.get("speaker_merges") or {}).items()
            if k in known and v in known
        })
        # A name belongs to a speaker that still exists after merging.
        names = {k: v for k, v in names.items() if k not in merges}

        entry = meeting_state.get(name)
        earlier = [Path(f.path) for f in entry.filed if f.project == project.name]
        result = file_meeting(
            meeting, project, names, title,
            speaker_merges=merges, replace=earlier[-1] if earlier else None,
        )

        meeting_state.update(
            name, title=title, speaker_names=names,
            speaker_merges=merges,
        )
        meeting_state.record_filed(name, project.name, result.path)
        self._needs_refresh = True
        return {"path": str(result.path), "project": project.name}

    def _api_settings(self, payload: dict) -> dict:
        s = self.settings
        s.model = str(payload.get("model", s.model))
        s.language = str(payload.get("language", s.language)).strip()
        s.silence_timeout = max(0, int(payload.get("silence_timeout", s.silence_timeout) or 0))
        count = max(0, int(payload.get("speaker_count", s.min_speakers) or 0))
        s.min_speakers = s.max_speakers = count
        s.mic = bool(payload.get("mic", s.mic))
        s.diarize = bool(payload.get("diarize", s.diarize))
        s.keep_recording = bool(payload.get("keep_recording", s.keep_recording))
        s.auto_process = bool(payload.get("auto_process", s.auto_process))
        save_settings(s)
        apply_ownscribe_config(s)
        return {"ok": True}

    def _meeting_directory(self, name: str) -> Path:
        # The name comes from the URL: accept only a direct child of the output dir.
        root = self.settings.resolved_output_dir
        directory = (root / name).resolve()
        if not name or directory.parent != root.resolve() or not directory.is_dir():
            raise ValueError(f"Unknown meeting {name!r}")
        return directory

    def _api_meeting_detail(self, name: str) -> dict:
        """The full transcript, for the dashboard's single-recording view."""
        meeting = parse_meeting_dir(self._meeting_directory(name))
        entry = meeting_state.get(name)
        turns = merge_speakers(meeting.turns, entry.speaker_merges)
        return {
            "name": name,
            "turns": [
                {"speaker": t.speaker, "start": t.start, "timestamp": t.timestamp, "text": t.text}
                for t in turns
            ],
            "markdown": render_turns(turns, entry.speaker_names),
        }

    def _api_rename_speaker(self, name: str, speaker: str, new_name: str) -> dict:
        """Rename one diarization label, from the transcript view's pencil icon.

        Writes straight to the ledger, same field the save form's "Who is who"
        edits -- so it carries over there, but (like that form) only reaches
        the saved copy the next time this meeting is filed.
        """
        meeting = parse_meeting_dir(self._meeting_directory(name))
        entry = meeting_state.get(name)
        known = set(speakers(merge_speakers(meeting.turns, entry.speaker_merges)))
        if speaker not in known:
            raise ValueError(f"Unknown speaker {speaker!r}")
        names = dict(entry.speaker_names)
        new_name = new_name.strip()
        if new_name:
            names[speaker] = new_name
        else:
            names.pop(speaker, None)
        meeting_state.update(name, speaker_names=names)
        return {"speaker_names": names}

    def _api_show(self) -> dict:
        """A second launch asks the running instance to come forward."""
        call_on_main(self._show_dashboard)
        return {"ok": True, "app": APP_ID}

    def _api_reveal(self, name: str) -> dict:
        directory = self._meeting_directory(name)
        subprocess.run(["open", str(directory)], check=False)
        return {"ok": True}

    def _api_dismiss(self, name: str) -> dict:
        """Mark a recording as not to be filed, so it stops counting as waiting."""
        self._meeting_directory(name)
        meeting_state.update(name, dismissed=True)
        self._needs_refresh = True
        return {"ok": True}

    def _api_forget(self, name: str) -> dict:
        meeting_state.forget(name)
        self._needs_refresh = True
        return {"ok": True}

    def _api_delete(self, name: str) -> dict:
        """Move a recording's folder to the Trash and forget what we knew of it.

        Notes already saved into projects are separate files and stay.
        """
        directory = self._meeting_directory(name)
        # Same test the list uses to show a recording as "processing".
        if self.recorder.state.is_active and self.recorder.current_dir == directory:
            raise ValueError("This recording is still being processed")
        move_to_trash(directory)
        meeting_state.forget(name)
        self._needs_refresh = True
        return {"ok": True}

    # -- projects ------------------------------------------------------------

    def _api_plan_project(self, payload: dict) -> dict:
        """What adding this project would create, for the setup form to show."""
        return plan_to_dict(plan_from_request(payload, self.settings.projects))

    def _api_add_project(self, payload: dict) -> dict:
        plan = plan_from_request(payload, self.settings.projects)
        created = apply_plan(plan)
        self.settings.projects.append(plan.project)
        save_settings(self.settings)
        return {"name": plan.project.name, "created": [display_path(p) for p in created]}

    def _api_remove_project(self, name: str) -> dict:
        """Forget a destination. Its folder and everything saved there stay."""
        project = self.settings.project(name)
        if project is None:
            raise ValueError(f"Unknown project {name!r}")
        self.settings.projects.remove(project)
        save_settings(self.settings)
        return {"ok": True}

    def _api_reveal_project(self, name: str) -> dict:
        project = self.settings.project(name)
        if project is None:
            raise ValueError(f"Unknown project {name!r}")
        folder = project.resolved_path
        subprocess.run(["open", str(folder if folder.is_dir() else folder.parent)], check=False)
        return {"ok": True}

    def _api_choose_folder(self, payload: dict) -> dict:
        """The native folder picker. Blocks this server thread, not the UI."""
        path = choose_folder(
            str(payload.get("prompt") or "Choose a folder"), str(payload.get("default") or "")
        )
        # The picker belongs to osascript; bring the FLY window back in front.
        call_on_main(activate)
        return {"path": path and display_path(Path(path))}

    def _api_record(self) -> dict:
        """Start or stop from the popover. Starting may show alerts, so it runs
        on the main thread rather than this server thread."""
        call_on_main(self.toggle_recording)
        return {"ok": True}

    def _api_process(self, name: str) -> dict:
        """Start processing a saved recording. Hops to the main thread, like
        recording, because it may show alerts."""
        directory = self._meeting_directory(name)
        if not parse_meeting_dir(directory).needs_processing:
            raise ValueError("This recording has nothing to process")
        if self.recorder.state.is_active:
            raise ValueError("Wait for the current recording to finish first")
        call_on_main(lambda: self.process_meeting(directory))
        return {"ok": True}

    def _api_check_update(self) -> dict:
        current = app_version()
        latest = latest_release_tag()
        if latest is None:
            return {
                "current": current, "latest": None, "available": False,
                "error": "Could not reach GitHub to check for updates.",
            }
        latest_number = latest.lstrip("v")
        available = current != "dev" and _version_tuple(latest) > _version_tuple(current)
        return {"current": current, "latest": latest_number, "available": available}

    def _api_apply_update(self) -> dict:
        """Hand off to install.sh, the same script a fresh install runs.

        Re-running it is the supported update path: it stops this app, installs
        the newest release and relaunches -- so this spawns it detached (it
        outlives this process once install.sh stops it) and returns right away.
        """
        run = self.recorder.state
        if run.is_active:
            raise ValueError("Let the current recording finish before updating.")
        if self._update_proc is not None and self._update_proc.poll() is None:
            return {"ok": True}  # already running
        UPDATE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with UPDATE_LOG.open("ab") as log:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            log.write(f"\n== Update from FLY v{app_version()}, {stamp} ==\n".encode())
            log.flush()
            self._update_offset = log.tell()
            # `arch -arm64`: if FLY was started under Rosetta (an older FLY.app
            # launcher did), its children inherit Intel mode and install.sh
            # refuses to run.
            self._update_proc = subprocess.Popen(
                ["/usr/bin/arch", "-arm64", "/bin/sh", "-c",
                 f"curl -fsSL {UPDATE_INSTALL_URL} | sh"],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        return {"ok": True}

    def _update_status(self) -> dict | None:
        """How an update started from here is going.

        A successful update stops this process and starts the new version, so
        all this can ever report is "running" -- or, if the installer exits
        while we are still here, that it didn't work and why.
        """
        proc = self._update_proc
        if proc is None:
            return None
        if proc.poll() is None:
            return {"status": "running"}
        try:
            with UPDATE_LOG.open("rb") as f:
                f.seek(self._update_offset)
                output = f.read().decode("utf-8", errors="replace")
        except OSError:
            output = ""
        return {
            "status": "failed",
            "error": installer_error(output),
            "log": str(UPDATE_LOG),
            "command": UPDATE_COMMAND,
        }

    # -- checks --------------------------------------------------------------

    def _warn_if_speakers_missing(self, state: RunState) -> None:
        """Surface a diarization that was asked for but did not happen.

        ownscribe reports this only as a log line and still exits 0, so without
        this check a transcript silently arrives with no speakers.
        """
        if not self.settings.diarize or state.meeting_dir is None:
            return
        meeting = parse_meeting_dir(state.meeting_dir)
        if not meeting.has_transcript or meeting.has_speakers:
            return
        access = check_diarization(appconfig.read_hf_token())
        detail = (
            access.reason
            if not access.ok
            else (
                "Diarization produced no speaker labels. The audio may have had "
                "only one speaker, or the model failed to load."
            )
        )
        alert("No speaker labels in transcript", detail)

    # -- menu actions --------------------------------------------------------

    def reload_settings(self, _sender) -> None:
        """Re-read settings.toml so project edits apply without a restart."""
        self.settings = load_settings()
        self.recorder._settings = self.settings
        self._refresh_awaiting()
        self._render(self.recorder.state)
        alert(
            "Settings reloaded",
            f"{len(self.settings.projects)} project(s) configured.",
        )

    def open_settings_file(self, _sender) -> None:
        appconfig.SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        if not appconfig.SETTINGS_PATH.exists():
            save_settings(self.settings)
        subprocess.run(["open", str(appconfig.SETTINGS_PATH)], check=False)

    def apply_config(self, _sender) -> None:
        try:
            path = apply_ownscribe_config(self.settings)
        except Exception as exc:
            alert("Configuration failed", str(exc))
            return
        alert("Configuration applied", f"Wrote {path}")

    def set_token(self, _sender) -> None:
        token = ask_text(
            "HuggingFace Token",
            "Only needed if the installer did not set up the speaker model.\n"
            "Paste a HuggingFace read token, after accepting the terms for "
            "pyannote/speaker-diarization-community-1.",
        )
        if not token:
            return
        path = appconfig.write_hf_token(token)
        apply_ownscribe_config(self.settings)
        alert("Token saved", f"Stored in {path} (owner-readable only).")

    def open_recordings(self, _sender) -> None:
        path = self.settings.resolved_output_dir
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["open", str(path)], check=False)

    def quit_app(self, _sender) -> None:
        if self._confirm_quit():
            self.shell.quit()

    def _confirm_quit(self) -> bool:
        """Ask before quitting mid-run; stop the run if the user agrees."""
        run = self.recorder.state
        if not run.is_active:
            return True
        if run.is_recording:
            title = "Recording in progress"
            message = "Quitting now discards the current recording. Quit anyway?"
        else:
            done = _processing_progress(run)
            how_far = f" (about {round(done * 100)}% done)" if done is not None else ""
            title = "Transcript still processing"
            message = (
                f"The recording is captured, but the transcript is not finished{how_far}. "
                "Quitting now loses that work. The audio stays, and you can "
                "process it again later. Quit anyway?"
            )
        if not alert(title, message, ok="Quit", cancel="Cancel"):
            return False
        self.recorder.abort()
        return True


def main() -> None:
    args = sys.argv[1:]
    if args == ["--version"]:
        print(f"fly-transcriber {app_version()}")
        return
    if args[:1] == ["install-agent"]:
        sys.exit(_install_agent(args[1:]))
    if args == ["warmup"]:
        _extend_path()
        sys.exit(_warmup())
    if args == ["install-launcher"]:
        sys.exit(_install_launcher())
    if args == ["uninstall-launcher"]:
        sys.exit(_uninstall_launcher())
    if args not in ([], ["--show"]):
        print(USAGE, file=sys.stderr)
        sys.exit(2)
    # Already running? Then this launch only brings that instance forward.
    if show_running_instance():
        sys.exit(0)
    _extend_path()
    MeetingRecorderApp().run(show=args == ["--show"])


#: Where ``uv tool install`` and Homebrew put executables. Started as a login
#: item, the app inherits launchd's minimal PATH, which has neither -- so
#: ownscribe and the ffmpeg it shells out to would not be found.
_TOOL_DIRS = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin")


#: Holds an ``ffmpeg`` link to the copy bundled with imageio-ffmpeg.
_FFMPEG_DIR = Path("~/.local/share/fly-transcriber/bin").expanduser()


def _extend_path() -> None:
    current = os.environ.get("PATH", "").split(os.pathsep)
    extra = [str(Path(d).expanduser()) for d in _TOOL_DIRS]
    missing = [d for d in extra if d not in current]
    if missing:
        os.environ["PATH"] = os.pathsep.join([*current, *missing])
    if shutil.which("ffmpeg") is None and (bundled := link_bundled_ffmpeg(_FFMPEG_DIR)):
        os.environ["PATH"] += os.pathsep + str(bundled.parent)


def link_bundled_ffmpeg(directory: Path) -> Path | None:
    """Link imageio-ffmpeg's binary as ``directory/ffmpeg``, so ownscribe finds it.

    ownscribe looks ffmpeg up on PATH by name; the bundled copy is named after
    its platform and version, and moves when the package is upgraded. Without
    it, people would need Homebrew just for ffmpeg. Returns None if unavailable.
    """
    try:
        import imageio_ffmpeg

        target = Path(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:  # noqa: BLE001 - missing package or binary
        return None
    link = directory / "ffmpeg"
    try:
        if link.is_symlink() and link.resolve() == target.resolve():
            return link
        directory.mkdir(parents=True, exist_ok=True)
        link.unlink(missing_ok=True)
        link.symlink_to(target)
    except OSError:
        return None
    return link


USAGE = """usage: fly-transcriber [--show]                start the menubar app (--show
                                               also opens the dashboard)
       fly-transcriber --version               print the installed version
       fly-transcriber install-agent <folder>  add CLAUDE.md and the agent skill
                                               to an existing project folder
       fly-transcriber warmup                  download the speech models now,
                                               not during the first recording
       fly-transcriber install-launcher        add FLY.app to Applications
       fly-transcriber uninstall-launcher      remove it again"""


def _install_launcher() -> int:
    try:
        path = install_launcher()
    except OSError as exc:
        print(f"Could not add FLY to Applications: {exc}", file=sys.stderr)
        return 1
    print(f"Added {path}")
    return 0


def _uninstall_launcher() -> int:
    for path in remove_launcher():
        print(f"Removed {path}")
    return 0


def _warmup() -> int:
    """Prefetch the Whisper and alignment models the next recording will use.

    Applies this app's ownscribe config first: ownscribe's own default enables
    local summarization, and warming up with it would download an LLM this app
    never runs. Diarization is skipped -- the installer places that model. With
    MLX the launcher's model loader runs, so it is the MLX weights that download.
    """
    settings = load_settings()
    apply_ownscribe_config(settings)
    prefix, _engine = ownscribe_command(settings.engine)
    cmd = [*prefix, "warmup", "--model", settings.model, "--no-diarization"]
    if settings.language:
        cmd += ["--language", settings.language]
    return subprocess.run(cmd, check=False).returncode


def _install_agent(args: list[str]) -> int:
    """Install the agent files into an existing project root."""
    if len(args) != 1:
        print(USAGE, file=sys.stderr)
        return 2
    base = Path(args[0]).expanduser().resolve()
    if not base.is_dir():
        print(f"Not a folder: {base}", file=sys.stderr)
        return 1
    written = install_agent_files(base, base.name)
    for path in written:
        print(f"Wrote {path}")
    if not written:
        print("Nothing to do: CLAUDE.md and the skill already exist.")
    return 0


if __name__ == "__main__":
    main()
