"""Serve the dashboard with fake data, for UI work without the menubar app.

    uv run python scripts/dashboard_preview.py   # http://127.0.0.1:8757/
                                                 # popover: /popover.html
"""

from __future__ import annotations

import os
import time

from fly_transcriber.projects import Project, display_path, plan_from_request, plan_to_dict
from fly_transcriber.server import Api, make_server

PORT = int(os.environ.get("PORT", 8757))

#: The fake run: processing at first, then idle; the record button starts a
#: recording, and stopping it processes for a few seconds, as the app would.
RUN = {"started": None, "stopped": time.time()}

TURNS = [
    ("SPEAKER_00", 3, "Onko teillä jo ne testitunnukset?"),
    ("SPEAKER_01", 6, "Ei vielä, odotetaan että API-avain tulee."),
    ("SPEAKER_01", 9, "Mä voin pingata sitä huomenna."),
    ("SPEAKER_00", 14, "Hyvä. Testitunnukset on sitten kunnossa, eikö?"),
    ("SPEAKER_02", 21, "Joo, ja ne pitää mapata ennen perjantaita."),
]

STATE = {
    "acme-sync": {"title": "Acme weekly sync",
                  "speaker_names": {"SPEAKER_00": "Aino"}, "pending_project": "Acme"},
}


def meeting(name, title, when, duration, speakers, filed=(), processing=False, transcript=True,
            unprocessed=False):
    entry = STATE.get(name, {})
    return {
        "name": name, "title": entry.get("title", title), "when": when, "duration": duration,
        "has_transcript": transcript, "has_audio": True, "processing": processing,
        "needs_processing": unprocessed,
        "speakers": speakers,
        "samples": {s: next(t for sp, _, t in TURNS if sp == s) for s in speakers},
        "filed": [{"project": p, "path": f"/Users/me/{p}/meetings/_inbox/28-09-26-{name}.md", "at": ""} for p in filed],
        "state": {"title": entry.get("title", ""),
                  "speaker_names": entry.get("speaker_names", {}),
                  "pending_project": entry.get("pending_project", ""),
                  "dismissed": entry.get("dismissed", False)},
    }


#: Remove both from the Settings page to see the no-projects flow.
PROJECTS = [
    Project("Acme", "/Users/me/acme/notes/meetings/_inbox", "vault", "obsidian"),
    Project("Globex", "/Users/me/globex/meetings/_inbox", "timestamp", "generic"),
]


def project_dict(p):
    return {"name": p.name, "path": p.path, "display": display_path(p.resolved_path),
            "naming": p.naming, "frontmatter": p.frontmatter}


def add_project(payload):
    # Plans against the real disk (read-only) but creates nothing.
    plan = plan_from_request(payload, PROJECTS)
    PROJECTS.append(plan.project)
    return {"name": plan.project.name, "created": []}


def remove_project(name):
    PROJECTS[:] = [p for p in PROJECTS if p.name != name]
    return {"ok": True}


def run_summary():
    if RUN["started"] is None:
        if time.time() - RUN["stopped"] > 8:
            return {"css": "idle", "label": "Idle", "detail": "", "elapsed": ""}
        return {"css": "busy", "label": "Diarizing", "detail": "42:10 captured", "elapsed": "42:10", "progress": 0.6}
    s = int(time.time() - RUN["started"])
    return {"css": "recording", "label": f"Recording — {s // 60}:{s % 60:02d}", "detail": "",
            "elapsed": f"{s // 60}:{s % 60:02d}"}


def record():
    if RUN["started"] is None:
        RUN["started"] = time.time()
    else:
        RUN["started"], RUN["stopped"] = None, time.time()
    return {"ok": True}


def check_update():
    return {"current": "0.6.2", "latest": "0.6.3", "available": True}


def apply_update():
    return {"ok": True}


def snapshot():
    three = ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"]
    return {
        "version": "0.6.2",
        "run": run_summary(),
        "meetings": [
            meeting("in-progress", "", "29.09. 10:00", 0, [], processing=True, transcript=False),
            meeting("standup", "", "29.09. 09:00", 900, [], transcript=False, unprocessed=True),
            meeting("acme-sync", "", "28.09. 14:20", 3420, three),
            meeting("design-review", "Design review", "27.09. 09:30", 1800, three[:2], filed=["Globex"]),
            meeting("one-on-one", "", "26.09. 13:00", 1500, three[:2], filed=["Acme", "Globex"]),
        ],
        "projects": [project_dict(p) for p in PROJECTS],
        "settings": {"model": "large-v3", "language": "", "silence_timeout": 300, "speaker_count": 0,
                     "mic": True, "diarize": True, "keep_recording": True, "auto_process": False},
    }


def detail(name):
    return {
        "name": name,
        "turns": [{"speaker": s, "start": t, "timestamp": f"00:{t:02d}", "text": x} for s, t, x in TURNS],
        "markdown": "\n".join(f"**{s}** {x}" for s, _, x in TURNS),
    }


def file_meeting(name, project, payload):
    STATE[name] = {**payload, "pending_project": project}
    return {"path": f"/Users/me/{project}/x.md", "project": project}


def dismiss(name):
    STATE[name] = {**STATE.get(name, {}), "dismissed": True}
    return {"ok": True}


def rename_speaker(name, speaker, new_name):
    names = {**STATE.get(name, {}).get("speaker_names", {})}
    if new_name.strip():
        names[speaker] = new_name.strip()
    else:
        names.pop(speaker, None)
    STATE[name] = {**STATE.get(name, {}), "speaker_names": names}
    return {"speaker_names": names}


if __name__ == "__main__":
    api = Api(snapshot=snapshot, file_meeting=file_meeting, save_settings=lambda p: {"ok": True},
              forget=lambda n: {"ok": True}, meeting_detail=detail, reveal=lambda n: {"ok": True},
              dismiss=dismiss, record=record, delete=lambda n: {"ok": True},
              plan_project=lambda p: plan_to_dict(plan_from_request(p, PROJECTS)),
              add_project=add_project, remove_project=remove_project,
              reveal_project=lambda n: {"ok": True},
              # No native picker here: pretend the user chose ~/Documents.
              choose_folder=lambda p: {"path": "~/Documents"},
              check_update=check_update, apply_update=apply_update, rename_speaker=rename_speaker,
              process=lambda n: {"ok": True})
    print(f"http://127.0.0.1:{PORT}/")
    make_server(api, PORT).serve_forever()
