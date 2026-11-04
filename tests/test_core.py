"""Tests for the non-UI logic: config generation, meeting parsing, export."""

from __future__ import annotations

import json
import sys
import time
import subprocess
import textwrap
import threading
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from fly_transcriber import config as appconfig
from fly_transcriber import diarization
from fly_transcriber import mlx_launch
from fly_transcriber import recorder as recorder_mod
from fly_transcriber import state as meeting_state
from fly_transcriber.server import Api, make_server
from fly_transcriber.config import Settings, build_ownscribe_config
from fly_transcriber.diarization import (
    DIARIZATION_REPO,
    LOCAL_MODEL_TOKEN,
    MODEL_FILES,
    check_access,
    check_diarization,
    has_local_model,
)
from fly_transcriber.dialogs import ask_list, ask_text, choose_folder
from fly_transcriber.filing import ATTRIBUTION_NOTE, FilingError, file_meeting
from fly_transcriber.transcript import Turn, load_turns, render, samples
from fly_transcriber.projects import (
    Project,
    SKILL_NAME,
    SKILL_RELPATH,
    DEFAULT_INBOX,
    apply_plan,
    create_project,
    install_agent_files,
    plan_from_request,
    plan_project,
    plan_to_dict,
    filename_for,
    slugify,
)
from fly_transcriber.meetings import (
    audio_duration,
    list_meetings,
    parse_meeting_dir,
    speaker_samples,
)
from fly_transcriber.recorder import Phase, Recorder, RunState

SUMMARY = textwrap.dedent(
    """\
    # Meeting Summary

    ## Decisions
    - Ship the integration on Friday.

    ## Open Questions
    - Who owns the API key rotation?

    ## To-Dos
    - Create the test accounts (SPEAKER_01).

    ## Summary
    Short overview of the meeting.
    """
)

TRANSCRIPT_DIARIZED = textwrap.dedent(
    """\
    # Transcript

    **SPEAKER_00** [00:03]
    Onko teillä jo ne testitunnukset?

    **SPEAKER_01** [00:12]
    Joo, ne pitää luoda erikseen.

    **SPEAKER_00** [00:20]
    Selvä.
    """
)

TRANSCRIPT_PLAIN = "# Transcript\n\n[00:03] Plain line with no speaker.\n"

TRANSCRIPT_WITH_UNKNOWN = textwrap.dedent(
    """\
    # Transcript

    **SPEAKER_00** [00:03]
    Onko teillä jo?

    **Unknown** [01:19]
    Eiko oikein?

    **SPEAKER_01** [01:30]
    Joo.
    """
)


def make_meeting(tmp_path, name="2026-09-28_1420_test-accounts", *, transcript, summary):
    d = tmp_path / name
    d.mkdir(parents=True)
    if transcript is not None:
        (d / "transcript.md").write_text(transcript, encoding="utf-8")
    if summary is not None:
        (d / "summary.md").write_text(summary, encoding="utf-8")
    return d


@pytest.fixture(autouse=True)
def no_local_model(tmp_path_factory, monkeypatch):
    """Keep a speaker model installed on this machine from leaking into tests."""
    monkeypatch.setattr(diarization, "MODELS_DIR", tmp_path_factory.mktemp("models"))


def install_fake_model(models_dir: Path) -> None:
    base = models_dir / DIARIZATION_REPO
    for name in MODEL_FILES:
        (base / name).parent.mkdir(parents=True, exist_ok=True)
        (base / name).write_bytes(b"x")


# -- ownscribe config generation ---------------------------------------------


def test_diarization_off_without_token(monkeypatch):
    """Claiming diarization without a token would silently produce no speakers."""
    monkeypatch.setattr(appconfig, "read_hf_token", lambda: "")
    doc = build_ownscribe_config(Settings(diarize=True))
    assert doc["diarization"]["enabled"] is False


def test_diarization_on_with_token(monkeypatch):
    monkeypatch.setattr(appconfig, "read_hf_token", lambda: "hf_dummy")
    doc = build_ownscribe_config(Settings(diarize=True))
    assert doc["diarization"]["enabled"] is True
    assert doc["diarization"]["hf_token"] == "hf_dummy"


def test_local_model_enables_diarization_without_token(monkeypatch):
    """ownscribe skips diarization without a token, so a placeholder stands in."""
    monkeypatch.setattr(appconfig, "read_hf_token", lambda: "")
    install_fake_model(diarization.MODELS_DIR)
    doc = build_ownscribe_config(Settings(diarize=True))
    assert doc["diarization"]["enabled"] is True
    assert doc["diarization"]["hf_token"] == LOCAL_MODEL_TOKEN


def test_real_token_wins_over_placeholder(monkeypatch):
    monkeypatch.setattr(appconfig, "read_hf_token", lambda: "hf_dummy")
    install_fake_model(diarization.MODELS_DIR)
    assert build_ownscribe_config(Settings(diarize=True))["diarization"]["hf_token"] == "hf_dummy"


def test_local_model_needs_every_file(tmp_path):
    assert not has_local_model(tmp_path)
    install_fake_model(tmp_path)
    assert has_local_model(tmp_path)
    (tmp_path / DIARIZATION_REPO / "plda" / "plda.npz").unlink()
    assert not has_local_model(tmp_path)


def test_check_diarization_accepts_local_model_without_network(tmp_path, monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("must not touch the network")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    install_fake_model(tmp_path)
    assert check_diarization("", tmp_path).ok


def test_check_diarization_without_model_or_token(tmp_path):
    result = check_diarization("", tmp_path)
    assert not result.ok
    assert "installer" in result.reason


def test_config_wires_model_and_disables_summarization(monkeypatch):
    """Summarization is off by design; the transcript is the product."""
    monkeypatch.setattr(appconfig, "read_hf_token", lambda: "")
    doc = build_ownscribe_config(Settings(model="large-v3", language="fi"))
    assert doc["summarization"]["enabled"] is False
    assert doc["transcription"]["model"] == "large-v3"
    assert doc["transcription"]["language"] == "fi"


def test_no_summarize_flag_passed():
    assert "--no-summarize" in Recorder(Settings())._cli_args()


def test_no_vocabulary_hints_reach_ownscribe(monkeypatch):
    """Fixing names and jargon is the note-writing agent's job, not Whisper's."""
    monkeypatch.setattr(appconfig, "read_hf_token", lambda: "")
    doc = build_ownscribe_config(Settings())
    assert doc["transcription"] == {"model": "large-v3", "language": ""}
    args = Recorder(Settings())._cli_args()
    assert "--hotwords" not in args and "--initial-prompt" not in args


def test_old_vocabulary_settings_are_ignored(tmp_path, monkeypatch):
    path = tmp_path / "settings.toml"
    path.write_text('hotwords = "Acme"\ninitial_prompt = "Globex"\nlanguage = "fi"\n', encoding="utf-8")
    monkeypatch.setattr(appconfig, "SETTINGS_PATH", path)
    settings = appconfig.load_settings()
    assert settings.language == "fi"
    assert settings.engine == "mlx"


def test_apply_backs_up_existing_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[audio]\nmic = false\n", encoding="utf-8")
    monkeypatch.setattr(appconfig, "OWNSCRIBE_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(appconfig, "OWNSCRIBE_CONFIG_PATH", cfg)
    monkeypatch.setattr(appconfig, "read_hf_token", lambda: "")

    appconfig.apply_ownscribe_config(Settings())

    backups = list(tmp_path.glob("config.toml.bak-*"))
    assert len(backups) == 1
    assert "mic = false" in backups[0].read_text(encoding="utf-8")


def test_token_file_is_owner_only(tmp_path, monkeypatch):
    monkeypatch.setattr(appconfig, "APP_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(appconfig, "TOKEN_PATH", tmp_path / "hf_token")
    path = appconfig.write_hf_token("hf_secret")
    assert path.stat().st_mode & 0o077 == 0
    monkeypatch.delenv(appconfig.HF_TOKEN_ENV, raising=False)
    assert appconfig.read_hf_token() == "hf_secret"


def test_env_token_wins_over_file(tmp_path, monkeypatch):
    monkeypatch.setattr(appconfig, "TOKEN_PATH", tmp_path / "hf_token")
    (tmp_path / "hf_token").write_text("from_file\n", encoding="utf-8")
    monkeypatch.setenv(appconfig.HF_TOKEN_ENV, "from_env")
    assert appconfig.read_hf_token() == "from_env"


# -- meeting parsing ---------------------------------------------------------


def test_parses_date_and_title(tmp_path):
    d = make_meeting(tmp_path, transcript=TRANSCRIPT_PLAIN, summary=SUMMARY)
    meeting = parse_meeting_dir(d)
    assert meeting.started is not None
    assert (meeting.started.year, meeting.started.hour) == (2026, 14)
    assert meeting.title == "test accounts"
    assert meeting.is_complete


def test_doubled_title_slug_is_readable(tmp_path):
    """Re-summarizing appends a second slug; the display name must stay legible."""
    d = make_meeting(
        tmp_path,
        name="2026-09-28_1420_login-flow_test-accounts-plan",
        transcript=None,
        summary=None,
    )
    assert parse_meeting_dir(d).title == "login flow test accounts plan"


def test_detects_speakers(tmp_path):
    d = make_meeting(tmp_path, transcript=TRANSCRIPT_DIARIZED, summary=SUMMARY)
    meeting = parse_meeting_dir(d)
    assert meeting.has_speakers
    assert meeting.speakers == ["SPEAKER_00", "SPEAKER_01"]


def _words_json(tmp_path, segments):
    """ownscribe-shaped JSON from [(segment speaker, [(word, word speaker), ...])]."""
    out, t = [], 0.0
    for seg_speaker, words in segments:
        ws = []
        for word, speaker in words:
            ws.append({"word": word, "start": t, "end": t + 0.3, "speaker": speaker})
            t += 0.4
        out.append({"text": " ".join(w for w, _ in words), "start": ws[0]["start"],
                    "end": ws[-1]["end"], "speaker": seg_speaker, "words": ws})
    path = tmp_path / "transcript.json"
    path.write_text(json.dumps({"segments": out}), encoding="utf-8")
    return path


def test_sentence_opening_goes_to_who_says_the_rest(tmp_path):
    """"Se" on the previous speaker, "on selvä…" on the next: one sentence, one speaker."""
    path = _words_json(tmp_path, [
        ("SPEAKER_00", [("Tehdään", "SPEAKER_00"), ("niin.", "SPEAKER_00")]),
        ("SPEAKER_00", [("Se", "SPEAKER_00")]),
        ("SPEAKER_01", [("on", "SPEAKER_01"), ("selvä", "SPEAKER_01"), ("käyttäjille.", "SPEAKER_01")]),
    ])
    turns = load_turns(path)
    assert [(t.speaker, t.text) for t in turns] == [
        ("SPEAKER_00", "Tehdään niin."),
        ("SPEAKER_01", "Se on selvä käyttäjille."),
    ]
    assert turns[1].start == pytest.approx(0.8)  # starts where "Se" does


def test_short_turns_that_are_their_own_sentence_stay(tmp_path):
    path = _words_json(tmp_path, [
        ("SPEAKER_00", [("Joo.", "SPEAKER_00")]),  # a complete answer
        ("SPEAKER_01", [("niin", "SPEAKER_01"), ("sitten", "SPEAKER_01"), ("eteenpäin.", "SPEAKER_01")]),
        ("SPEAKER_00", [("Okei", "SPEAKER_00")]),  # next turn starts a new sentence
        ("SPEAKER_01", [("Mennään", "SPEAKER_01"), ("eteenpäin.", "SPEAKER_01")]),
    ])
    assert [t.speaker for t in load_turns(path)] == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00", "SPEAKER_01"]


def test_unknown_is_not_a_speaker(tmp_path):
    """ownscribe marks unattributed segments "Unknown"; it is not a person."""
    d = make_meeting(tmp_path, transcript=TRANSCRIPT_WITH_UNKNOWN, summary=None)
    meeting = parse_meeting_dir(d)
    assert meeting.speakers == ["SPEAKER_00", "SPEAKER_01"]
    assert "Unknown" not in speaker_samples(meeting)
    # The marker stays in the transcript body, where it flags a real gap.
    assert "**Unknown**" in meeting.transcript_path.read_text(encoding="utf-8")


def test_only_unknown_counts_as_undiarized(tmp_path):
    d = make_meeting(
        tmp_path,
        transcript="# Transcript\n\n**Unknown** [00:01]\nHello.\n",
        summary=None,
    )
    assert parse_meeting_dir(d).has_speakers is False


def test_plain_transcript_has_no_speakers(tmp_path):
    d = make_meeting(tmp_path, transcript=TRANSCRIPT_PLAIN, summary=SUMMARY)
    assert parse_meeting_dir(d).speakers == []


def test_incomplete_meeting(tmp_path):
    d = make_meeting(tmp_path, name="2026-09-28_1409", transcript=None, summary=None)
    meeting = parse_meeting_dir(d)
    assert not meeting.is_complete
    assert meeting.title == ""


def test_transcript_alone_is_complete(tmp_path):
    """Summarization is disabled, so no summary.md is ever produced."""
    d = make_meeting(tmp_path, transcript=TRANSCRIPT_DIARIZED, summary=None)
    assert parse_meeting_dir(d).is_complete


def test_unparseable_dir_name_is_tolerated(tmp_path):
    d = make_meeting(tmp_path, name="scratch", transcript=None, summary=None)
    meeting = parse_meeting_dir(d)
    assert meeting.started is None


def test_list_meetings_is_newest_first(tmp_path):
    make_meeting(tmp_path, "2026-09-27_0900_older", transcript=None, summary=None)
    make_meeting(tmp_path, "2026-09-28_1420_newer", transcript=None, summary=None)
    names = [m.directory.name for m in list_meetings(tmp_path)]
    assert names == ["2026-09-28_1420_newer", "2026-09-27_0900_older"]


def test_list_meetings_missing_dir(tmp_path):
    assert list_meetings(tmp_path / "nope") == []


# -- projects and filing -----------------------------------------------------


def test_vault_naming_is_dd_mm_yy(tmp_path):
    d = make_meeting(
        tmp_path,
        name="2026-09-28_1420_acme-design-review",
        transcript=TRANSCRIPT_DIARIZED,
        summary=SUMMARY,
    )
    meeting = parse_meeting_dir(d)
    project = Project(name="Acme", path=str(tmp_path / "inbox"), naming="vault")
    assert filename_for(meeting, project) == "28-09-26-acme-design-review.md"


def test_timestamp_naming_preserves_directory_name(tmp_path):
    d = make_meeting(tmp_path, transcript=TRANSCRIPT_DIARIZED, summary=SUMMARY)
    meeting = parse_meeting_dir(d)
    project = Project(name="X", path=str(tmp_path), naming="timestamp")
    assert filename_for(meeting, project) == f"{d.name}.md"


def test_slugify():
    assert slugify("Acme UX Review!") == "acme-ux-review"
    assert slugify("") == "meeting"


def test_slugify_transliterates_finnish():
    """Naive ASCII folding would turn 'Ääkköset' into 'kk-set'."""
    assert slugify("Ääkköset ja työvuorot") == "aakkoset-ja-tyovuorot"
    assert slugify("Käyttäjähaastattelut") == "kayttajahaastattelut"


def test_file_meeting_writes_obsidian_frontmatter(tmp_path):
    d = make_meeting(tmp_path / "src", transcript=TRANSCRIPT_DIARIZED, summary=SUMMARY)
    meeting = parse_meeting_dir(d)
    project = Project(name="Acme", path=str(tmp_path / "inbox"), frontmatter="obsidian")

    result = file_meeting(meeting, project)
    note = result.path.read_text(encoding="utf-8")

    assert result.path.parent == (tmp_path / "inbox")
    assert "type: meeting" in note
    assert "date: 28-09-26" in note  # DD-MM-YY, not ISO
    assert "status: raw" in note
    # Anonymous diarization labels are not participants.
    assert "participants: []" in note
    assert "speakers: [SPEAKER_00, SPEAKER_01]" in note
    assert "## Transcript" in note
    assert "**SPEAKER_01**" in note


def test_filed_note_warns_it_is_unreviewed(tmp_path):
    """The receiving agent must be able to tell machine output from a real note."""
    d = make_meeting(tmp_path / "src", transcript=TRANSCRIPT_DIARIZED, summary=SUMMARY)
    project = Project(name="Acme", path=str(tmp_path / "inbox"), frontmatter="obsidian")
    note = file_meeting(parse_meeting_dir(d), project).path.read_text(encoding="utf-8")
    assert "not reviewed by a human" in note


def test_filing_never_overwrites(tmp_path):
    d = make_meeting(tmp_path / "src", transcript=TRANSCRIPT_DIARIZED, summary=SUMMARY)
    meeting = parse_meeting_dir(d)
    project = Project(name="Acme", path=str(tmp_path / "inbox"))

    first = file_meeting(meeting, project).path
    second = file_meeting(meeting, project).path

    assert first != second
    assert second.name.endswith("-2.md")
    assert first.exists() and second.exists()


def test_filing_creates_missing_directory(tmp_path):
    d = make_meeting(tmp_path / "src", transcript=TRANSCRIPT_DIARIZED, summary=SUMMARY)
    project = Project(name="Acme", path=str(tmp_path / "a" / "b" / "inbox"))
    assert file_meeting(parse_meeting_dir(d), project).path.exists()


def test_filing_refuses_meeting_without_transcript(tmp_path):
    d = make_meeting(tmp_path / "src", transcript=None, summary=None)
    project = Project(name="Acme", path=str(tmp_path / "inbox"))
    with pytest.raises(FilingError):
        file_meeting(parse_meeting_dir(d), project)


def test_title_drives_filename(tmp_path):
    """Without summarization nothing else can name the file."""
    d = make_meeting(tmp_path / "src", transcript=TRANSCRIPT_DIARIZED, summary=None)
    project = Project(name="Acme", path=str(tmp_path / "inbox"))
    result = file_meeting(parse_meeting_dir(d), project, title="Acme UX Review")
    assert result.path.name == "28-09-26-acme-ux-review.md"


def test_untitled_meeting_falls_back_to_time(tmp_path):
    """A generic slug would collide across meetings on the same day."""
    d = tmp_path / "src" / "2026-09-28_1420"
    d.mkdir(parents=True)
    (d / "transcript.md").write_text(TRANSCRIPT_DIARIZED, encoding="utf-8")
    project = Project(name="Acme", path=str(tmp_path / "inbox"))
    assert file_meeting(parse_meeting_dir(d), project).path.name == (
        "28-09-26-meeting-1420.md"
    )


def test_generic_frontmatter_uses_iso_date(tmp_path):
    d = make_meeting(tmp_path / "src", transcript=TRANSCRIPT_DIARIZED, summary=SUMMARY)
    project = Project(name="X", path=str(tmp_path / "out"), frontmatter="generic")
    note = file_meeting(parse_meeting_dir(d), project).path.read_text(encoding="utf-8")
    assert "date: 2026-09-28" in note


def test_project_roundtrip_through_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(appconfig, "APP_CONFIG_DIR", tmp_path)
    monkeypatch.setattr(appconfig, "SETTINGS_PATH", tmp_path / "settings.toml")

    original = Settings(
        projects=[
            Project(name="Acme", path="~/acme/notes/meetings/_inbox", tags=["acme"]),
            Project(name="Globex", path="~/globex", naming="timestamp"),
        ]
    )
    appconfig.save_settings(original)
    loaded = appconfig.load_settings()

    assert [p.name for p in loaded.projects] == ["Acme", "Globex"]
    assert loaded.project("Acme").tags == ["acme"]
    assert loaded.project("Globex").naming == "timestamp"
    assert loaded.project("missing") is None


def test_invalid_project_values_fall_back():
    """A hand-edited settings file must not break startup."""
    p = Project.from_dict(
        {"name": "X", "path": "/tmp", "naming": "bogus", "frontmatter": "bogus", "x": 1}
    )
    assert p.naming == "vault"
    assert p.frontmatter == "generic"


# -- participants and speaker naming -----------------------------------------


def test_speaker_samples_gives_one_line_each(tmp_path):
    d = make_meeting(tmp_path, transcript=TRANSCRIPT_DIARIZED, summary=SUMMARY)
    samples = speaker_samples(parse_meeting_dir(d))
    assert list(samples) == ["SPEAKER_00", "SPEAKER_01"]
    assert samples["SPEAKER_00"].startswith("Onko")
    assert samples["SPEAKER_01"].startswith("Joo")


def test_samples_prefer_longest_turn_over_small_talk():
    turns = [
        Turn("SPEAKER_00", 0, "Hei!"),
        Turn("SPEAKER_01", 2, "Moi, mitä kuuluu?"),
        Turn("SPEAKER_00", 5, "Hyvää. Aloitetaan budjetista, Globexin tarjous tuli eilen."),
        Turn("SPEAKER_01", 9, "Joo."),
        Turn("SPEAKER_01", 11, "Kävin sen läpi, hinnat ovat samat kuin viime vuonna."),
        Turn("SPEAKER_01", 15, "Kävin myös sopimuksen läpi, ehdot ovat samat."),
        Turn("Unknown", 20, "pitkä mutta kenenkään ei tunnistettu puhe tässä kohdassa nyt"),
    ]
    found = samples(turns)
    assert list(found) == ["SPEAKER_00", "SPEAKER_01"]
    assert found["SPEAKER_00"].startswith("Hyvää. Aloitetaan")
    # Equal length: the earlier turn wins.
    assert found["SPEAKER_01"].startswith("Kävin sen läpi")


def test_samples_clip_at_a_word_boundary():
    text = "Aloitetaan budjetista, koska Globexin tarjous tuli eilen ja se pitää käydä läpi"
    clipped = samples([Turn("SPEAKER_00", 0, text)], max_chars=40)["SPEAKER_00"]
    assert len(clipped) <= 40
    assert clipped.endswith("…")
    assert text.startswith(clipped[:-1])
    assert clipped[-2] != " "


def test_speaker_samples_empty_without_diarization(tmp_path):
    d = make_meeting(tmp_path, transcript=TRANSCRIPT_PLAIN, summary=SUMMARY)
    assert speaker_samples(parse_meeting_dir(d)) == {}


def test_participants_are_the_named_speakers(tmp_path):
    d = make_meeting(tmp_path / "src", transcript=TRANSCRIPT_DIARIZED, summary=SUMMARY)
    project = Project(name="Acme", path=str(tmp_path / "inbox"), frontmatter="obsidian")
    note = file_meeting(
        parse_meeting_dir(d), project, speaker_names={"SPEAKER_00": "Sam Virtanen"}
    ).path.read_text(encoding="utf-8")
    # SPEAKER_01 stays anonymous, so it is a speaker but not a participant.
    assert "participants: [Sam Virtanen]" in note
    assert "speakers: [Sam Virtanen, SPEAKER_01]" in note


def test_participants_follow_merges(tmp_path):
    d = make_meeting(tmp_path / "src", transcript=TRANSCRIPT_DIARIZED, summary=SUMMARY)
    project = Project(name="Acme", path=str(tmp_path / "inbox"), frontmatter="obsidian")
    note = file_meeting(
        parse_meeting_dir(d), project,
        speaker_names={"SPEAKER_00": "Sam"},
        speaker_merges={"SPEAKER_01": "SPEAKER_00"},
    ).path.read_text(encoding="utf-8")
    assert "participants: [Sam]" in note


def test_speaker_names_rewrite_transcript(tmp_path):
    d = make_meeting(tmp_path / "src", transcript=TRANSCRIPT_DIARIZED, summary=SUMMARY)
    project = Project(name="Acme", path=str(tmp_path / "inbox"))
    note = file_meeting(
        parse_meeting_dir(d),
        project,
        speaker_names={"SPEAKER_00": "Sam", "SPEAKER_01": "Alex"},
    ).path.read_text(encoding="utf-8")
    assert "**Sam**" in note and "**Alex**" in note
    assert "**SPEAKER_00**" not in note
    assert "speakers: [Sam, Alex]" in note


def test_partial_speaker_mapping_keeps_unmapped_labels(tmp_path):
    d = make_meeting(tmp_path / "src", transcript=TRANSCRIPT_DIARIZED, summary=SUMMARY)
    project = Project(name="Acme", path=str(tmp_path / "inbox"))
    note = file_meeting(
        parse_meeting_dir(d), project, speaker_names={"SPEAKER_00": "Sam"}
    ).path.read_text(encoding="utf-8")
    assert "**Sam**" in note
    assert "**SPEAKER_01**" in note  # unmapped stays anonymous


def test_speaker_rename_does_not_touch_source_transcript(tmp_path):
    """A wrong mapping must never corrupt the original transcript."""
    d = make_meeting(tmp_path / "src", transcript=TRANSCRIPT_DIARIZED, summary=SUMMARY)
    meeting = parse_meeting_dir(d)
    original = meeting.transcript_path.read_text(encoding="utf-8")
    file_meeting(
        meeting,
        Project(name="X", path=str(tmp_path / "inbox")),
        speaker_names={"SPEAKER_00": "Sam"},
    )
    assert meeting.transcript_path.read_text(encoding="utf-8") == original


def test_diarized_note_cautions_about_attribution(tmp_path):
    d = make_meeting(tmp_path / "src", transcript=TRANSCRIPT_DIARIZED, summary=SUMMARY)
    project = Project(name="X", path=str(tmp_path / "inbox"), frontmatter="generic")
    note = file_meeting(parse_meeting_dir(d), project).path.read_text(encoding="utf-8")
    assert ATTRIBUTION_NOTE in note


def test_undiarized_note_has_no_attribution_caution(tmp_path):
    d = make_meeting(tmp_path / "src", transcript=TRANSCRIPT_PLAIN, summary=SUMMARY)
    project = Project(name="X", path=str(tmp_path / "inbox"))
    note = file_meeting(parse_meeting_dir(d), project).path.read_text(encoding="utf-8")
    assert ATTRIBUTION_NOTE not in note


def test_render_ignores_blank_names():
    turns = [Turn("SPEAKER_00", 3.0, "hello")]
    assert "**SPEAKER_00**" in render(turns, {})


# -- project creation --------------------------------------------------------


def test_create_project_makes_inbox_and_instructions(tmp_path):
    project, written = create_project("Acme Corp", root=tmp_path)
    assert project.name == "Acme Corp"
    assert (tmp_path / "acme-corp" / "meetings" / "_inbox").is_dir()
    instructions = tmp_path / "acme-corp" / "CLAUDE.md"
    assert instructions in written
    text = instructions.read_text(encoding="utf-8")
    assert "status: raw" in text
    # The scaffold must tell the agent that writing notes is its job.
    assert "does no\nsummarization" in text
    # ...and warn about the transcription errors it will encounter.
    assert "Speech recognition errors" in text
    assert SKILL_NAME in text


def test_create_project_installs_skill(tmp_path):
    _, written = create_project("Acme", root=tmp_path)
    skill = tmp_path / "acme" / SKILL_RELPATH
    assert skill in written
    text = skill.read_text(encoding="utf-8")
    assert text.startswith("---\nname: fly-summarise\n")
    assert "Attribution review" in text


def test_scaffold_explains_speaker_attribution(tmp_path):
    create_project("Acme", root=tmp_path)
    text = (tmp_path / "acme" / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Correct an attribution only on clear evidence" in text


def test_create_project_never_clobbers_existing_agent_files(tmp_path):
    base = tmp_path / "acme"
    (base / SKILL_RELPATH).parent.mkdir(parents=True)
    (base / "CLAUDE.md").write_text("my own instructions", encoding="utf-8")
    (base / SKILL_RELPATH).write_text("my own skill", encoding="utf-8")
    _, written = create_project("Acme", root=tmp_path)
    assert written == []
    assert (base / "CLAUDE.md").read_text(encoding="utf-8") == "my own instructions"
    assert (base / SKILL_RELPATH).read_text(encoding="utf-8") == "my own skill"


def test_install_agent_files_into_existing_folder(tmp_path):
    written = install_agent_files(tmp_path, "Vault")
    assert written == [tmp_path / "CLAUDE.md", tmp_path / SKILL_RELPATH]
    assert install_agent_files(tmp_path, "Vault") == []


def test_create_project_without_scaffold(tmp_path):
    _, written = create_project("Bare", root=tmp_path, scaffold=False)
    assert written == []
    assert not (tmp_path / "bare" / "CLAUDE.md").exists()
    assert not (tmp_path / "bare" / SKILL_RELPATH).exists()


def test_create_project_rejects_empty_name(tmp_path):
    with pytest.raises(ValueError):
        create_project("   ", root=tmp_path)


def test_create_project_is_idempotent(tmp_path):
    create_project("Acme", root=tmp_path)
    project, _ = create_project("Acme", root=tmp_path)
    assert Path(project.path).is_dir()


# -- project setup -----------------------------------------------------------


def test_plan_new_project_lists_everything_it_creates(tmp_path):
    plan = plan_project("Acme Corp", tmp_path, new=True)
    root = tmp_path / "acme-corp"
    assert plan.root == root
    assert [(s.path, s.exists) for s in plan.steps] == [
        (root, False),
        (root / DEFAULT_INBOX, False),
        (root / "CLAUDE.md", False),
        (root / SKILL_RELPATH, False),
    ]
    assert plan.project.path == str(root / DEFAULT_INBOX)
    assert not root.exists()  # planning touches nothing


def test_apply_plan_creates_what_was_planned(tmp_path):
    plan = plan_project("Acme", tmp_path, new=True)
    created = apply_plan(plan)
    assert set(created) == {s.path for s in plan.steps}
    assert all(s.path.exists() for s in plan.steps)


def test_plan_new_project_refuses_an_existing_folder(tmp_path):
    (tmp_path / "acme").mkdir()
    with pytest.raises(ValueError, match="already exists"):
        plan_project("Acme", tmp_path, new=True)


def test_plan_existing_folder_keeps_existing_files(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("mine", encoding="utf-8")
    plan = plan_project("Vault", tmp_path, new=False)
    kept = {s.path.name: s.exists for s in plan.steps}
    assert kept == {"_inbox": False, "CLAUDE.md": True, "SKILL.md": False}
    apply_plan(plan)
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == "mine"
    assert not plan.notes  # default inbox: the skill finds it anyway


def test_plan_warns_when_kept_claude_md_cannot_know_custom_inbox(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("mine", encoding="utf-8")
    plan = plan_project("Vault", tmp_path, new=False, inbox="Inbox")
    assert "Inbox/" in plan.notes[0]


def test_plan_existing_folder_without_inbox_or_agent_files(tmp_path):
    plan = plan_project("Vault", tmp_path, new=False, inbox="", agent_files=False)
    assert plan.steps == []
    assert plan.project.path == str(tmp_path)


@pytest.mark.parametrize("inbox", ["../elsewhere", "/abs", "~/x", "a/../../b"])
def test_plan_keeps_inbox_inside_the_project(tmp_path, inbox):
    with pytest.raises(ValueError):
        plan_project("Vault", tmp_path, new=False, inbox=inbox)


def test_plan_refuses_home_folder_and_missing_folders(tmp_path):
    with pytest.raises(ValueError, match="home folder"):
        plan_project("Home", Path.home(), new=False)
    with pytest.raises(ValueError, match="not a folder"):
        plan_project("Gone", tmp_path / "missing", new=False)
    with pytest.raises(ValueError, match="name"):
        plan_project("  ", tmp_path, new=True)


def test_plan_from_request_refuses_duplicates(tmp_path):
    existing = [Project("Acme", str(tmp_path / "vault" / DEFAULT_INBOX))]
    (tmp_path / "vault").mkdir()
    with pytest.raises(ValueError, match="already a project named"):
        plan_from_request({"kind": "new", "name": "acme", "folder": str(tmp_path)}, existing)
    with pytest.raises(ValueError, match="already saves to"):
        plan_from_request({"kind": "existing", "name": "Other", "folder": str(tmp_path / "vault")}, existing)


def test_plan_to_dict_abbreviates_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    data = plan_to_dict(plan_project("Acme", tmp_path, new=True))
    assert data["root"] == "~/acme"
    assert data["steps"][1]["path"] == f"~/acme/{DEFAULT_INBOX}"


def test_choose_folder_returns_path_or_none(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run("/Users/me/Acme/\n"))
    assert choose_folder("Pick") == "/Users/me/Acme"
    monkeypatch.setattr(subprocess, "run", _fake_run("", returncode=1))
    assert choose_folder("Pick") is None


# -- recorder ----------------------------------------------------------------


def test_cli_args_reflect_settings():
    rec = Recorder(Settings(model="large-v3", language="fi", diarize=True, mic=False))
    args = rec._cli_args()
    assert args[:3] == ["--no-summarize", "--model", "large-v3"]
    assert "--diarize" in args
    assert "--no-mic" in args
    assert "fi" in args


def test_recordings_not_kept_by_default():
    """WAVs run ~300 MB/hour, so keeping them is opt-in."""
    assert Settings().keep_recording is False
    assert "--no-keep-recording" in Recorder(Settings())._cli_args()


def test_keep_recording_can_be_turned_on():
    args = Recorder(Settings(keep_recording=True))._cli_args()
    assert "--keep-recording" in args


def test_keep_recording_reaches_ownscribe_config(monkeypatch):
    monkeypatch.setattr(appconfig, "read_hf_token", lambda: "")
    assert build_ownscribe_config(Settings())["output"]["keep_recording"] is False
    assert (
        build_ownscribe_config(Settings(keep_recording=True))["output"][
            "keep_recording"
        ]
        is True
    )


def test_json_output_requested(monkeypatch):
    """Word-level speakers exist only in the JSON output."""
    monkeypatch.setattr(appconfig, "read_hf_token", lambda: "")
    assert build_ownscribe_config(Settings())["output"]["format"] == "json"
    args = Recorder(Settings())._cli_args()
    assert args[args.index("--format") + 1] == "json"


def test_cli_args_omit_diarize_when_disabled():
    assert "--diarize" not in Recorder(Settings(diarize=False))._cli_args()


# -- transcription engine -----------------------------------------------------


def _ownscribe_script(tmp_path, monkeypatch, shebang=f"#!{sys.executable}"):
    script = tmp_path / "ownscribe"
    script.write_text(f"{shebang}\nfrom ownscribe.cli import cli\n", encoding="utf-8")
    monkeypatch.setattr(
        recorder_mod.shutil, "which", lambda name: str(script) if name == "ownscribe" else None
    )
    return script


def test_ownscribe_python_comes_from_its_shebang(tmp_path):
    script = tmp_path / "ownscribe"
    script.write_text(f"#!{sys.executable}\n", encoding="utf-8")
    assert recorder_mod.ownscribe_python(str(script)) == sys.executable
    script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")  # not a uv tool
    assert recorder_mod.ownscribe_python(str(script)) is None
    assert recorder_mod.ownscribe_python(str(tmp_path / "missing")) is None


def test_mlx_is_used_when_installed(tmp_path, monkeypatch):
    _ownscribe_script(tmp_path, monkeypatch)
    monkeypatch.setattr(recorder_mod, "has_mlx", lambda python: True)
    prefix, engine = recorder_mod.ownscribe_command("mlx")
    assert engine == "mlx"
    assert prefix == [sys.executable, "-P", str(recorder_mod.MLX_LAUNCHER)]
    assert recorder_mod.MLX_LAUNCHER.is_file()


def test_falls_back_to_ownscribe_whisper(tmp_path, monkeypatch):
    script = _ownscribe_script(tmp_path, monkeypatch)
    monkeypatch.setattr(recorder_mod, "has_mlx", lambda python: False)
    assert recorder_mod.ownscribe_command("mlx") == ([str(script)], "faster-whisper")
    monkeypatch.setattr(recorder_mod, "has_mlx", lambda python: True)
    assert recorder_mod.ownscribe_command("faster-whisper") == ([str(script)], "faster-whisper")


def test_has_mlx_probes_the_environment(tmp_path):
    assert recorder_mod.has_mlx(sys.executable) is False  # FLY's own env has no MLX
    assert recorder_mod.has_mlx(str(tmp_path / "no-python")) is False


def test_mlx_result_is_shaped_for_whisperx():
    result = {
        "language": "fi",
        "segments": [
            {"start": 0.0, "end": 2.0, "text": " Moi.", "tokens": [1, 2],
             "words": [{"word": " Moi.", "start": 0.1, "end": 0.6, "probability": 0.9},
                       {"word": " ", "start": 0.6, "end": 0.7, "probability": 0.1}]},
            {"start": 2.0, "end": 30.0, "text": " ", "words": []},  # silence
        ],
    }
    assert mlx_launch.to_whisperx(result) == {
        "segments": [{"start": 0.0, "end": 2.0, "text": " Moi.",
                      "words": [{"word": "Moi.", "start": 0.1, "end": 0.6, "score": 0.9}]}],
        "language": "fi",
    }


def test_mlx_launcher_skips_alignment(monkeypatch):
    """No per-language alignment model: not when recording, not at warmup."""
    calls = []

    class FakeTranscriber:
        def _should_align(self):
            return True

        def _prepare_transcription_models(self, **kwargs):
            calls.append(kwargs)

    module = types.ModuleType("ownscribe.transcription.whisperx_transcriber")
    module.WhisperXTranscriber = FakeTranscriber
    monkeypatch.setitem(sys.modules, "ownscribe", types.ModuleType("ownscribe"))
    monkeypatch.setitem(sys.modules, "ownscribe.transcription", types.ModuleType("ownscribe.transcription"))
    monkeypatch.setitem(sys.modules, "ownscribe.transcription.whisperx_transcriber", module)

    printed = []
    click = types.ModuleType("click")
    click.echo = lambda message=None, *a, **k: printed.append(message)
    monkeypatch.setitem(sys.modules, "click", click)

    mlx_launch.install()
    transcriber = FakeTranscriber()
    assert transcriber._should_align() is False
    click.echo("Whisper model ready: large-v3")
    click.echo("Alignment model not preloaded (language auto-detect).")
    assert printed == ["Whisper model ready: large-v3"]  # no claims about a model there isn't
    transcriber._prepare_transcription_models(language="fi", step_key="preparing_models",
                                              show_deferred_align_note=True)
    assert calls == [{"language": "fi", "step_key": "preparing_models",
                      "show_deferred_align_note": False, "load_align": False}]


def test_mlx_decodes_each_window_on_its_own(monkeypatch):
    """Conditioning on the previous window made large-v3 loop and drop sentences."""
    calls = {}
    fake = types.ModuleType("mlx_whisper")

    def transcribe(audio, **options):
        calls.update(options, audio=audio)
        return {"language": "fi", "segments": [{"start": 0.0, "end": 1.0, "text": " Joo."}]}

    fake.transcribe = transcribe
    monkeypatch.setitem(sys.modules, "mlx_whisper", fake)
    monkeypatch.setattr(mlx_launch, "_release", lambda: calls.setdefault("released", True))

    out = mlx_launch.MlxModel("/models/large-v3", "fi").transcribe(
        "audio", batch_size=16, print_progress=True, combined_progress=True
    )
    assert calls["condition_on_previous_text"] is False
    assert calls["word_timestamps"] is True  # diarization assigns speakers per word
    assert calls["path_or_hf_repo"] == "/models/large-v3"
    assert calls["language"] == "fi"
    assert calls["released"]
    assert out["segments"][0]["text"] == " Joo."


def test_mlx_weights_come_from_the_cache_first():
    asked = []

    def uncached(repo, local_files_only=False):
        asked.append(local_files_only)
        if local_files_only:
            raise OSError("not cached")
        return "/downloaded"

    assert mlx_launch.resolve_model("r", uncached) == "/downloaded"
    assert asked == [True, False]
    assert mlx_launch.resolve_model("r", lambda repo, local_files_only=False: "/cache") == "/cache"


def test_mlx_repo_follows_the_model_setting():
    assert mlx_launch.repo_for("large-v3") == "mlx-community/whisper-large-v3-mlx"


def test_progress_parsing_advances_phases():
    rec = Recorder(Settings())
    rec.state = RunState(phase=Phase.STARTING)

    rec._interpret("  Recording: 01:05")
    assert rec.state.phase is Phase.RECORDING
    assert rec.state.elapsed == 65

    rec._interpret("Transcribing")
    assert rec.state.phase is Phase.TRANSCRIBING

    rec._interpret("Summarizing")
    assert rec.state.phase is Phase.SUMMARIZING


def test_phases_never_go_backwards():
    """The output log is cumulative, so an earlier marker must not rewind state."""
    rec = Recorder(Settings())
    rec.state = RunState(phase=Phase.SUMMARIZING)
    rec._interpret("Transcribing")
    assert rec.state.phase is Phase.SUMMARIZING


def test_run_state_flags():
    assert RunState(phase=Phase.RECORDING).is_recording
    assert RunState(phase=Phase.TRANSCRIBING).is_active
    assert not RunState(phase=Phase.TRANSCRIBING).is_recording
    assert not RunState(phase=Phase.DONE).is_active
    assert not RunState(phase=Phase.IDLE).is_active


# -- dialogs -----------------------------------------------------------------


def _fake_run(stdout="", returncode=0):
    def run(cmd, **kwargs):
        run.script = cmd[-1]
        return subprocess.CompletedProcess(cmd, returncode, stdout, "")

    return run


def test_ask_text_returns_entered_value(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run("Acme UX Review\n"))
    assert ask_text("t", "m") == "Acme UX Review"


def test_ask_text_none_on_cancel(monkeypatch):
    """A cancelled osascript dialog exits non-zero."""
    monkeypatch.setattr(subprocess, "run", _fake_run("", returncode=1))
    assert ask_text("t", "m") is None


def test_ask_text_none_on_empty(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run("  \n"))
    assert ask_text("t", "m") is None


def test_ask_text_handles_skip_button(monkeypatch):
    """Skip is an ordinary button, so the script must check which was pressed."""
    run = _fake_run("")
    monkeypatch.setattr(subprocess, "run", run)
    ask_text("t", "m")
    assert 'if button returned of reply is "Skip" then return' in run.script
    assert "if gave up of reply then return" in run.script


def test_reply_is_bound_to_a_variable(monkeypatch):
    """AppleScript reassigns `result` after every statement.

    Reading the dialog reply from `result` means the first `if` that inspects it
    destroys it, and the next line dies with "variable result is not defined" --
    which the caller cannot distinguish from the user pressing Skip. The typed
    answer is silently discarded.
    """
    run = _fake_run("x")
    monkeypatch.setattr(subprocess, "run", run)
    ask_text("t", "m")
    assert "set reply to display dialog" in run.script
    assert "of result" not in run.script


def test_enter_selects_ok_not_skip(monkeypatch):
    """Enter activates the default button; it must be OK."""
    run = _fake_run("x")
    monkeypatch.setattr(subprocess, "run", run)
    ask_text("t", "m")
    assert 'default button "OK"' in run.script


def test_dialog_activates_itself(monkeypatch):
    """Without activation the dialog cannot take keyboard focus."""
    run = _fake_run("x")
    monkeypatch.setattr(subprocess, "run", run)
    ask_text("t", "m")
    assert "tell me to activate" in run.script


def test_quotes_are_escaped(monkeypatch):
    """An unescaped quote would break the AppleScript, not just the text."""
    run = _fake_run("x")
    monkeypatch.setattr(subprocess, "run", run)
    ask_text('He said "hi"', "back\\slash")
    assert '\\"hi\\"' in run.script
    assert "back\\\\slash" in run.script


def test_ask_list_splits_and_strips(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run(" Sam , Alex ,,\n"))
    assert ask_list("t", "m") == ["Sam", "Alex"]


def test_ask_list_empty_on_skip(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run("", returncode=1))
    assert ask_list("t", "m") == []


def test_ask_text_survives_timeout(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired("osascript", 1)

    monkeypatch.setattr(subprocess, "run", boom)
    assert ask_text("t", "m") is None


# -- transcript turns --------------------------------------------------------


def _json_transcript(tmp_path, segments):
    import json

    d = tmp_path / "2026-09-28_1420"
    d.mkdir(parents=True, exist_ok=True)
    (d / "transcript.json").write_text(
        json.dumps({"segments": segments, "language": "fi"}), encoding="utf-8"
    )
    return d


def _w(text, start, speaker):
    return {"word": text, "start": start, "end": start + 0.3, "speaker": speaker}


def test_segment_splits_where_word_speaker_changes(tmp_path):
    """The whole point: ownscribe collapses this into one speaker block."""
    d = _json_transcript(
        tmp_path,
        [
            {
                "text": "En ma tieda. Mita mulle tulee mieleen?",
                "start": 11.0,
                "speaker": "SPEAKER_00",
                "words": [
                    _w("En", 11.0, "SPEAKER_00"),
                    _w("ma", 11.3, "SPEAKER_00"),
                    _w("tieda.", 11.6, "SPEAKER_00"),
                    _w("Mita", 12.0, "SPEAKER_01"),
                    _w("mulle", 12.3, "SPEAKER_01"),
                    _w("tulee", 12.6, "SPEAKER_01"),
                ],
            }
        ],
    )
    turns = load_turns(d / "transcript.json")
    assert [t.speaker for t in turns] == ["SPEAKER_00", "SPEAKER_01"]
    assert turns[0].text == "En ma tieda."
    assert turns[1].text.startswith("Mita mulle")


def test_single_word_flip_does_not_split(tmp_path):
    """A one-word flip mid-sentence is a boundary artefact, not a turn."""
    d = _json_transcript(
        tmp_path,
        [
            {
                "text": "a b c d",
                "start": 0.0,
                "speaker": "SPEAKER_00",
                "words": [
                    _w("a", 0.0, "SPEAKER_00"),
                    _w("b", 0.3, "SPEAKER_00"),
                    _w("c", 0.6, "SPEAKER_01"),
                    _w("d", 0.9, "SPEAKER_00"),
                ],
            }
        ],
    )
    turns = load_turns(d / "transcript.json")
    assert [t.speaker for t in turns] == ["SPEAKER_00"]


def test_same_speaker_shares_one_block_but_keeps_timestamps(tmp_path):
    """One label, but every line keeps its own time -- as ownscribe rendered."""
    d = _json_transcript(
        tmp_path,
        [
            {"text": "one", "start": 0.0, "speaker": "S0", "words": [_w("one", 0.0, "S0")]},
            {"text": "two", "start": 61.0, "speaker": "S0", "words": [_w("two", 61.0, "S0")]},
        ],
    )
    out = render(load_turns(d / "transcript.json"))
    assert out.count("**S0**") == 1
    assert "[01:01] two" in out


def test_segment_without_words_kept_whole(tmp_path):
    d = _json_transcript(
        tmp_path, [{"text": "unaligned text", "start": 5.0, "speaker": "S0"}]
    )
    turns = load_turns(d / "transcript.json")
    assert [t.text for t in turns] == ["unaligned text"]


def test_json_preferred_over_markdown(tmp_path):
    d = _json_transcript(
        tmp_path, [{"text": "from json", "start": 0.0, "speaker": "S0"}]
    )
    (d / "transcript.md").write_text("# Transcript\n\n[00:00] from markdown\n", "utf-8")
    meeting = parse_meeting_dir(d)
    assert meeting.transcript_path.name == "transcript.json"
    assert "from json" in render(meeting.turns)


def test_markdown_still_readable(tmp_path):
    """Recordings made before the JSON switch must still file."""
    d = make_meeting(tmp_path / "legacy", transcript=TRANSCRIPT_DIARIZED, summary=None)
    meeting = parse_meeting_dir(d)
    assert meeting.speakers == ["SPEAKER_00", "SPEAKER_01"]
    assert meeting.has_transcript


def test_render_applies_names(tmp_path):
    turns = [Turn("SPEAKER_00", 3.0, "moi"), Turn("SPEAKER_01", 9.0, "hei")]
    out = render(turns, {"SPEAKER_00": "Alex"})
    assert "**Alex** [00:03]" in out
    assert "**SPEAKER_01** [00:09]" in out


def test_timestamps_formatted_from_seconds():
    assert Turn("S", 125.0, "x").timestamp == "02:05"


# -- filing ledger -----------------------------------------------------------


def test_ledger_records_filing(tmp_path, monkeypatch):
    monkeypatch.setattr(meeting_state, "STATE_PATH", tmp_path / "state.json")
    meeting_state.update("m1", title="Design review")
    meeting_state.record_filed("m1", "Acme", Path("/x/28-09-26-design-review.md"))

    entry = meeting_state.get("m1")
    assert entry.is_filed
    assert entry.title == "Design review"
    assert entry.filed[0].project == "Acme"
    assert entry.filed[0].at  # timestamped


def test_ledger_clears_pending_on_file(tmp_path, monkeypatch):
    monkeypatch.setattr(meeting_state, "STATE_PATH", tmp_path / "state.json")
    meeting_state.update("m1", pending_project="Acme")
    assert meeting_state.get("m1").pending_project == "Acme"
    meeting_state.record_filed("m1", "Acme", Path("/x/a.md"))
    assert meeting_state.get("m1").pending_project == ""


def test_awaiting_filing_skips_filed_dismissed_and_unfinished(tmp_path, monkeypatch):
    monkeypatch.setattr(meeting_state, "STATE_PATH", tmp_path / "state.json")
    src = tmp_path / "src"
    for name in ("2026-09-28_0900", "2026-09-28_1000", "2026-09-28_1100"):
        make_meeting(src, name, transcript=TRANSCRIPT_DIARIZED, summary=None)
    make_meeting(src, "2026-09-28_1200", transcript=None, summary=None)  # processing
    meeting_state.record_filed("2026-09-28_0900", "Acme", Path("/x/a.md"))
    meeting_state.update("2026-09-28_1000", dismissed=True)

    waiting = meeting_state.awaiting_filing(list_meetings(src))
    assert [m.directory.name for m in waiting] == ["2026-09-28_1100"]


def test_filing_a_skipped_meeting_unskips_it(tmp_path, monkeypatch):
    monkeypatch.setattr(meeting_state, "STATE_PATH", tmp_path / "state.json")
    meeting_state.update("m1", dismissed=True)
    meeting_state.record_filed("m1", "Acme", Path("/x/a.md"))
    assert meeting_state.get("m1").dismissed is False


def test_ledger_unknown_meeting_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(meeting_state, "STATE_PATH", tmp_path / "state.json")
    assert meeting_state.get("nope").is_filed is False


def test_corrupt_ledger_does_not_raise(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(meeting_state, "STATE_PATH", path)
    # A broken ledger must never block recording or filing.
    assert meeting_state.load_all() == {}
    meeting_state.update("m1", title="x")
    assert meeting_state.get("m1").title == "x"


def test_ledger_write_is_atomic(tmp_path, monkeypatch):
    """A half-written ledger would lose every meeting's filing history."""
    path = tmp_path / "state.json"
    monkeypatch.setattr(meeting_state, "STATE_PATH", path)
    meeting_state.update("m1", title="x")
    assert not path.with_suffix(".json.tmp").exists()
    assert json.loads(path.read_text(encoding="utf-8"))["m1"]["title"] == "x"


# -- web server --------------------------------------------------------------


def _client(api):
    server = make_server(api, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    return f"http://127.0.0.1:{port}", server


def _stub_api(**overrides):
    calls = {}

    def snapshot():
        return {"run": {"label": "Idle"}, "meetings": [], "projects": [], "settings": {}}

    def file_meeting(meeting, project, payload):
        calls["file"] = (meeting, project, payload)
        return {"path": "/x/a.md", "project": project}

    def save_settings(payload):
        calls["settings"] = payload
        return {"ok": True}

    def forget(meeting):
        calls["forget"] = meeting
        return {"ok": True}

    api = Api(
        snapshot=overrides.get("snapshot", snapshot),
        file_meeting=overrides.get("file_meeting", file_meeting),
        save_settings=overrides.get("save_settings", save_settings),
        forget=overrides.get("forget", forget),
        show=overrides.get("show"),
        delete=overrides.get("delete"),
        check_update=overrides.get("check_update"),
        apply_update=overrides.get("apply_update"),
        rename_speaker=overrides.get("rename_speaker"),
    )
    return api, calls


def test_server_serves_state_and_static():
    api, _ = _stub_api()
    base, server = _client(api)
    try:
        assert json.loads(urllib.request.urlopen(base + "/api/state").read())["run"]
        page = urllib.request.urlopen(base + "/").read().decode()
        assert 'id="root"' in page
        vendor = urllib.request.urlopen(base + "/vendor/htm-preact-3.1.1.js")
        assert vendor.headers["Content-Type"].startswith("text/javascript")
    finally:
        server.shutdown()


def test_server_serves_meeting_detail():
    api, _ = _stub_api()
    api.meeting_detail = lambda name: {"name": name, "turns": [], "markdown": ""}
    base, server = _client(api)
    try:
        data = json.loads(urllib.request.urlopen(base + "/api/meeting?name=2026-09-28_1420").read())
        assert data["name"] == "2026-09-28_1420"
    finally:
        server.shutdown()


def test_server_meeting_detail_errors_are_404():
    api, _ = _stub_api()
    base, server = _client(api)
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(base + "/api/meeting?name=x")
        assert exc.value.code == 404
    finally:
        server.shutdown()


def test_server_rejects_path_traversal():
    """The requested path comes from the URL and must stay inside static/."""
    api, _ = _stub_api()
    base, server = _client(api)
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(base + "/../config.py")
        assert exc.value.code == 404
    finally:
        server.shutdown()


def test_server_files_meeting():
    api, calls = _stub_api()
    base, server = _client(api)
    try:
        body = json.dumps(
            {"meeting": "m1", "project": "Acme", "title": "T", "speaker_names": {"SPEAKER_00": "Sam"}}
        ).encode()
        req = urllib.request.Request(
            base + "/api/file", data=body, headers={"Content-Type": "application/json"}
        )
        assert json.loads(urllib.request.urlopen(req).read())["path"] == "/x/a.md"
        assert calls["file"][0] == "m1"
        assert calls["file"][2]["speaker_names"] == {"SPEAKER_00": "Sam"}
    finally:
        server.shutdown()


def test_server_routes_project_requests():
    api, calls = _stub_api()
    api.plan_project = lambda payload: calls.setdefault("plan", payload) and {"steps": []}
    api.remove_project = lambda name: calls.setdefault("remove", name) and {"ok": True}
    base, server = _client(api)
    try:
        for path, body in (("/api/project/plan", {"name": "Acme"}), ("/api/project/remove", {"name": "Acme"})):
            req = urllib.request.Request(
                base + path, data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req).read()
        assert calls["plan"] == {"name": "Acme"}
        assert calls["remove"] == "Acme"
    finally:
        server.shutdown()


def test_server_routes_update_requests():
    api, calls = _stub_api()
    api.check_update = lambda: {"current": "0.6.2", "latest": "0.6.3", "available": True}
    api.apply_update = lambda: calls.setdefault("apply", True) and {"ok": True}
    base, server = _client(api)
    try:
        req = urllib.request.Request(
            base + "/api/update/check", data=b"{}", headers={"Content-Type": "application/json"}
        )
        assert json.loads(urllib.request.urlopen(req).read()) == {
            "current": "0.6.2", "latest": "0.6.3", "available": True,
        }
        req = urllib.request.Request(
            base + "/api/update/apply", data=b"{}", headers={"Content-Type": "application/json"}
        )
        assert json.loads(urllib.request.urlopen(req).read()) == {"ok": True}
        assert calls["apply"] is True
    finally:
        server.shutdown()


def test_server_routes_rename_speaker_requests():
    api, calls = _stub_api()
    api.rename_speaker = lambda meeting, speaker, name: (
        calls.setdefault("rename", (meeting, speaker, name)) and {"speaker_names": {speaker: name}}
    )
    base, server = _client(api)
    try:
        req = urllib.request.Request(
            base + "/api/speaker-name",
            data=json.dumps({"meeting": "m1", "speaker": "SPEAKER_00", "name": "Sam"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        assert json.loads(urllib.request.urlopen(req).read()) == {"speaker_names": {"SPEAKER_00": "Sam"}}
        assert calls["rename"] == ("m1", "SPEAKER_00", "Sam")
    finally:
        server.shutdown()


def test_server_dismisses_meeting():
    api, calls = _stub_api()
    api.dismiss = lambda meeting: calls.setdefault("dismiss", meeting) and {"ok": True}
    base, server = _client(api)
    try:
        req = urllib.request.Request(
            base + "/api/dismiss",
            data=json.dumps({"meeting": "m1"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        assert json.loads(urllib.request.urlopen(req).read()) == {"ok": True}
        assert calls["dismiss"] == "m1"
    finally:
        server.shutdown()


def test_server_toggles_recording():
    api, calls = _stub_api()

    def record():
        calls["record"] = calls.get("record", 0) + 1
        return {"ok": True}

    api.record = record
    base, server = _client(api)
    try:
        req = urllib.request.Request(
            base + "/api/record", data=b"{}", method="POST",
            headers={"Content-Type": "application/json"},
        )
        assert json.loads(urllib.request.urlopen(req).read()) == {"ok": True}
        assert calls["record"] == 1
    finally:
        server.shutdown()


def test_server_serves_popover_page():
    api, _ = _stub_api()
    base, server = _client(api)
    try:
        page = urllib.request.urlopen(base + "/popover.html").read().decode()
        assert "/popover.js" in page
        shared = urllib.request.urlopen(base + "/common.js")
        assert shared.headers["Content-Type"].startswith("text/javascript")
    finally:
        server.shutdown()


def test_server_reports_errors_as_json():
    """A blank 500 in the browser would give no clue what went wrong."""

    def boom(*_a):
        raise ValueError("Unknown project 'Nope'")

    api, _ = _stub_api(file_meeting=boom)
    base, server = _client(api)
    try:
        req = urllib.request.Request(
            base + "/api/file", data=b"{}", headers={"Content-Type": "application/json"}
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req)
        assert "Unknown project" in json.loads(exc.value.read())["error"]
    finally:
        server.shutdown()


def test_server_binds_loopback_only():
    api, _ = _stub_api()
    server = make_server(api, port=0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def test_progress_prediction_adapts_to_history(tmp_path, monkeypatch):
    from fly_transcriber import progress

    monkeypatch.setattr(progress, "TIMINGS_PATH", tmp_path / "timings.json")
    assert progress.predict(1000) == pytest.approx(30 + 1000 * 0.37)

    progress.record(59, 50)  # nearly all start-up: too short to learn from
    assert not (tmp_path / "timings.json").exists()
    progress.record(600, 150)
    progress.record(1200, 270)
    assert progress.predict(3000) == pytest.approx(30 + 3000 * 0.2)


def test_progress_counts_start_up_time(tmp_path, monkeypatch):
    """A 5-minute MLX recording took 112 s; per-second scaling alone predicted 50 s
    and the bar sat at its end for a minute."""
    from fly_transcriber import progress

    monkeypatch.setattr(progress, "TIMINGS_PATH", tmp_path / "timings.json")
    assert progress.predict(315, "mlx") == pytest.approx(112, rel=0.15)
    progress.record(315, 112, "mlx")
    assert progress.predict(315, "mlx") == pytest.approx(112)


def test_progress_ignores_old_short_samples(tmp_path, monkeypatch):
    from fly_transcriber import progress

    monkeypatch.setattr(progress, "TIMINGS_PATH", tmp_path / "timings.json")
    (tmp_path / "timings.json").write_text("[[31, 205.3]]", encoding="utf-8")  # a first-run download
    assert progress.predict(1000) == pytest.approx(30 + 1000 * 0.37)


def test_progress_keeps_timings_per_engine(tmp_path, monkeypatch):
    """MLX is ~4x faster; CPU timings must not make its estimates crawl."""
    from fly_transcriber import progress

    monkeypatch.setattr(progress, "TIMINGS_PATH", tmp_path / "timings.json")
    progress.record(600, 240)  # faster-whisper
    assert progress.predict(1000, "mlx") == pytest.approx(60 + 1000 * 0.12)
    progress.record(600, 120, "mlx")
    assert (tmp_path / "timings-mlx.json").exists()
    assert progress.predict(1000, "mlx") == pytest.approx(60 + 1000 * 0.1)
    assert progress.predict(1000) == pytest.approx(30 + 1000 * 0.35)


def test_progress_keeps_moving_past_the_prediction():
    from fly_transcriber import progress

    assert progress.fraction(0, 100) == 0
    assert progress.fraction(5, 0) == 0
    assert progress.fraction(50, 100) == pytest.approx(0.45)
    assert progress.fraction(100, 100) == pytest.approx(progress.DUE)
    assert progress.fraction(200, 100) == pytest.approx((progress.DUE + progress.CAP) / 2)
    later = [progress.fraction(t, 100) for t in (100, 150, 300, 1000, 10**6)]
    assert later == sorted(later) and len(set(later)) == len(later)
    assert later[-1] < progress.CAP < 1


def test_merged_speakers_become_one_in_the_saved_note(tmp_path):
    d = make_meeting(tmp_path / "src", transcript=TRANSCRIPT_DIARIZED, summary=SUMMARY)
    meeting = parse_meeting_dir(d)
    project = Project(name="Acme", path=str(tmp_path / "inbox"), frontmatter="obsidian")

    note = file_meeting(
        meeting, project, speaker_names={"SPEAKER_00": "Aino"},
        speaker_merges={"SPEAKER_01": "SPEAKER_00"},
    ).path.read_text(encoding="utf-8")

    assert "speakers: [Aino]" in note
    assert "SPEAKER_01" not in note
    assert note.count("**Aino**") == 1  # consecutive turns collapse into one block


def test_resolve_merges_follows_chains_and_ignores_cycles():
    from fly_transcriber.transcript import resolve_merges

    assert resolve_merges({"B": "A", "C": "B"}) == {"B": "A", "C": "A"}
    assert resolve_merges({"A": "A"}) == {}
    assert resolve_merges({"A": "B", "B": "A"}) == {}


def test_refiling_rewrites_a_raw_note_in_place(tmp_path):
    d = make_meeting(tmp_path / "src", transcript=TRANSCRIPT_DIARIZED, summary=SUMMARY)
    meeting = parse_meeting_dir(d)
    project = Project(name="Acme", path=str(tmp_path / "inbox"), frontmatter="obsidian")

    first = file_meeting(meeting, project).path
    again = file_meeting(meeting, project, title="Renamed", replace=first).path
    assert again == first
    assert "title: Renamed" in first.read_text(encoding="utf-8")
    assert len(list((tmp_path / "inbox").iterdir())) == 1


def test_refiling_leaves_a_note_the_agent_already_processed(tmp_path):
    d = make_meeting(tmp_path / "src", transcript=TRANSCRIPT_DIARIZED, summary=SUMMARY)
    meeting = parse_meeting_dir(d)
    project = Project(name="Acme", path=str(tmp_path / "inbox"), frontmatter="obsidian")

    first = file_meeting(meeting, project).path
    first.write_text("# Finished notes\n", encoding="utf-8")  # marker removed
    second = file_meeting(meeting, project, replace=first).path
    assert second != first
    assert first.read_text(encoding="utf-8") == "# Finished notes\n"


def test_ledger_refiling_same_path_does_not_duplicate(tmp_path, monkeypatch):
    monkeypatch.setattr(meeting_state, "STATE_PATH", tmp_path / "state.json")
    meeting_state.record_filed("m1", "Acme", Path("/x/a.md"))
    meeting_state.record_filed("m1", "Acme", Path("/x/a.md"))
    assert len(meeting_state.get("m1").filed) == 1


def test_launcher_bundle_runs_the_tool_with_show(tmp_path):
    import plistlib
    import subprocess

    from fly_transcriber import launcher

    marker = tmp_path / "args"
    tool = tmp_path / "fly tool"  # a space: the path must be quoted
    tool.write_text(f'#!/bin/sh\necho "$@" > "{marker}"\n')
    tool.chmod(0o755)

    app = launcher.install_launcher((tmp_path / "Apps",), tool)

    assert app == tmp_path / "Apps" / "FLY.app"
    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleIdentifier"] == launcher.BUNDLE_ID
    assert info["LSUIElement"] is True  # no Dock icon
    assert (app / "Contents" / "Resources" / "FLY.icns").exists()
    subprocess.run([str(app / "Contents" / "MacOS" / "FLY")], check=True)
    # The tool is started in the background, so give it a moment to write.
    for _ in range(50):
        if marker.exists() and marker.read_text().strip():
            break
        time.sleep(0.1)
    assert marker.read_text().strip() == "--show"


def test_launcher_falls_back_when_first_folder_is_not_writable(tmp_path):
    from fly_transcriber import launcher

    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        app = launcher.install_launcher((locked, tmp_path / "home"), tmp_path / "tool")
    finally:
        locked.chmod(0o700)
    assert app.parent == tmp_path / "home"
    assert not (locked / "FLY.app").exists()


def test_launcher_reinstall_moves_rather_than_duplicates(tmp_path):
    from fly_transcriber import launcher

    first, second = tmp_path / "a", tmp_path / "b"
    launcher.install_launcher((first, second), tmp_path / "tool")
    assert (first / "FLY.app").exists()
    first.chmod(0o500)  # now unwritable: the second install lands in b
    try:
        launcher.install_launcher((first, second), tmp_path / "tool")
    finally:
        first.chmod(0o700)
    assert (second / "FLY.app").exists()


def test_launcher_removal_only_touches_our_bundle(tmp_path):
    from fly_transcriber import launcher

    foreign = tmp_path / "FLY.app" / "Contents"
    foreign.mkdir(parents=True)
    (foreign / "Info.plist").write_bytes(b"not ours")
    assert launcher.remove_launcher((tmp_path,)) == []
    assert foreign.exists()

    (tmp_path / "FLY.app").rename(tmp_path / "other.app")
    launcher.install_launcher((tmp_path,), tmp_path / "tool")
    assert launcher.remove_launcher((tmp_path,)) == [tmp_path / "FLY.app"]
    assert (tmp_path / "other.app").exists()


def test_second_launch_brings_the_running_app_forward():
    from fly_transcriber.server import APP_ID, show_running_instance

    shown = []
    api, _ = _stub_api(show=lambda: shown.append(1) or {"ok": True, "app": APP_ID})
    base, server = _client(api)
    try:
        assert show_running_instance(port=server.server_address[1])
        assert shown == [1]
    finally:
        server.shutdown()


def test_show_ignores_a_stranger_on_the_port():
    from fly_transcriber.server import show_running_instance

    api, _ = _stub_api(show=lambda: {"ok": True})  # answers, but is not FLY
    base, server = _client(api)
    try:
        assert not show_running_instance(port=server.server_address[1])
    finally:
        server.shutdown()
    assert not show_running_instance(port=1)  # nothing listening


def test_delete_route_reaches_the_api():
    deleted = []
    api, _ = _stub_api(delete=lambda name: deleted.append(name) or {"ok": True})
    base, server = _client(api)
    try:
        req = urllib.request.Request(
            base + "/api/delete", data=b'{"meeting": "2026-09-28_0900"}',
            headers={"Content-Type": "application/json"},
        )
        assert json.loads(urllib.request.urlopen(req).read()) == {"ok": True}
        assert deleted == ["2026-09-28_0900"]
    finally:
        server.shutdown()


def _status(req) -> int:
    try:
        return urllib.request.urlopen(req).status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_server_refuses_requests_from_other_sites():
    calls = []
    api, _ = _stub_api(delete=lambda n: calls.append(n) or {"ok": True})
    base, server = _client(api)
    post = lambda headers: urllib.request.Request(  # noqa: E731
        base + "/api/delete", data=b'{"meeting": "m"}', method="POST", headers=headers
    )
    try:
        # A web page's "simple" cross-site POST: no preflight, any content type.
        assert _status(post({"Content-Type": "text/plain", "Origin": "https://evil.example"})) == 403
        # Even with a JSON body, a foreign Origin is refused.
        assert _status(post({"Content-Type": "application/json", "Origin": "https://evil.example"})) == 403
        # Not JSON at all.
        assert _status(post({"Content-Type": "text/plain"})) == 403
        assert _status(post({})) == 403
        assert calls == []
        # The app's own pages still work, with or without an Origin header.
        ok = {"Content-Type": "application/json"}
        assert _status(post(ok)) == 200
        assert _status(post({**ok, "Origin": base})) == 200
        assert calls == ["m", "m"]
    finally:
        server.shutdown()


def test_server_refuses_foreign_host_names():
    """DNS rebinding: a page under evil.example that resolves to 127.0.0.1."""
    api, _ = _stub_api()
    base, server = _client(api)
    try:
        for path in ("/api/state", "/", "/style.css"):
            req = urllib.request.Request(base + path, headers={"Host": "evil.example"})
            assert _status(req) == 403
        localhost = f"localhost:{server.server_address[1]}"
        assert _status(urllib.request.Request(base + "/api/state", headers={"Host": localhost})) == 200
    finally:
        server.shutdown()


def test_bundled_ffmpeg_is_linked_and_runs(tmp_path):
    from fly_transcriber.app import link_bundled_ffmpeg

    link = link_bundled_ffmpeg(tmp_path / "bin")
    assert link == tmp_path / "bin" / "ffmpeg"
    assert link_bundled_ffmpeg(tmp_path / "bin") == link  # idempotent
    out = subprocess.run([str(link), "-version"], capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.startswith("ffmpeg version")


def test_version_tuple_parses_and_rejects():
    from fly_transcriber.app import _version_tuple

    assert _version_tuple("v0.10.0") > _version_tuple("v0.9.0")  # numeric, not lexical
    assert _version_tuple("0.6.2") == (0, 6, 2)
    assert _version_tuple("main") == ()
    assert _version_tuple("dev") == ()


def test_latest_release_tag_prefers_newest_numeric_tag(monkeypatch):
    from fly_transcriber import app as app_module

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(self._payload).encode()

    tags = [{"name": "speaker-model-v1"}, {"name": "v0.6.2"}, {"name": "v0.10.0"}, {"name": "v0.9.0"}]
    monkeypatch.setattr(app_module.urllib.request, "urlopen", lambda *a, **k: FakeResponse(tags))
    assert app_module.latest_release_tag() == "v0.10.0"


def test_latest_release_tag_falls_back_to_releases_latest(monkeypatch):
    from fly_transcriber import app as app_module

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(self._payload).encode()

    def fake_urlopen(request, timeout=None):
        if "tags" in request.full_url:
            raise OSError("rate limited")
        return FakeResponse({"tag_name": "v0.7.0"})

    monkeypatch.setattr(app_module.urllib.request, "urlopen", fake_urlopen)
    assert app_module.latest_release_tag() == "v0.7.0"


def test_latest_release_tag_none_when_unreachable(monkeypatch):
    from fly_transcriber import app as app_module

    def fake_urlopen(*_args, **_kwargs):
        raise OSError("offline")

    monkeypatch.setattr(app_module.urllib.request, "urlopen", fake_urlopen)
    assert app_module.latest_release_tag() is None


# -- hardening -----------------------------------------------------------------


@pytest.mark.parametrize("frontmatter", ["obsidian", "generic"])
def test_frontmatter_survives_titles_and_names_people_type(tmp_path, frontmatter):
    import yaml

    d = make_meeting(tmp_path / "src", transcript=TRANSCRIPT_DIARIZED, summary=SUMMARY)
    project = Project(
        name="Acme", path=str(tmp_path / "inbox"), frontmatter=frontmatter, tags=["acme: q4"]
    )
    title = 'Acme: kickoff #3 [draft]\nstatus: done'
    names = {"SPEAKER_00": "Aino: host", "SPEAKER_01": "O'Brien, the PM"}
    note = file_meeting(
        parse_meeting_dir(d), project, title=title, speaker_names=names,
    ).path.read_text(encoding="utf-8")

    meta = yaml.safe_load(note.split("---")[1])
    assert meta["title"] == "Acme: kickoff #3 [draft] status: done"
    assert meta["status"] == "raw"  # a newline in the title cannot add a field
    assert meta["participants"] == list(names.values())
    assert meta["speakers"] == list(names.values())
    assert meta["source"] == str(d)
    assert "# Acme: kickoff #3 [draft] status: done\n" in note


def test_yaml_str_leaves_plain_text_bare():
    from fly_transcriber.filing import yaml_str

    assert yaml_str("Design review") == "Design review"
    assert yaml_str("Q&A with O'Brien (UX)") == "Q&A with O'Brien (UX)"
    assert yaml_str("Ääkköset") == "Ääkköset"
    assert yaml_str("Acme: kickoff") == '"Acme: kickoff"'
    assert yaml_str("no") == '"no"'
    assert yaml_str("") == '""'


def test_server_pages_cannot_be_framed_by_other_sites():
    api, _ = _stub_api()
    base, server = _client(api)
    try:
        for path in ("/", "/popover.html", "/api/state"):
            headers = urllib.request.urlopen(base + path).headers
            assert headers["X-Frame-Options"] == "SAMEORIGIN"
            assert "frame-ancestors 'self'" in headers["Content-Security-Policy"]
            assert headers["X-Content-Type-Options"] == "nosniff"
    finally:
        server.shutdown()


def test_server_ignores_bad_or_huge_content_length():
    import socket

    calls = []
    api, _ = _stub_api(delete=lambda n: calls.append(n) or {"ok": True})
    base, server = _client(api)
    port = server.server_address[1]
    try:
        for length in ("nonsense", "999999999"):
            with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
                sock.sendall(
                    f"POST /api/delete HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                    f"Content-Type: application/json\r\nContent-Length: {length}\r\n\r\n".encode()
                )
                assert b" 200 " in sock.recv(4096).split(b"\r\n")[0] + b" "
        assert calls == ["", ""]  # read as an empty request, not a hang or crash
    finally:
        server.shutdown()


def test_generated_ownscribe_config_is_not_backed_up_again(tmp_path, monkeypatch):
    import tomllib

    cfg = tmp_path / "ownscribe" / "config.toml"
    monkeypatch.setattr(appconfig, "OWNSCRIBE_CONFIG_DIR", cfg.parent)
    monkeypatch.setattr(appconfig, "OWNSCRIBE_CONFIG_PATH", cfg)
    monkeypatch.setattr(appconfig, "read_hf_token", lambda: "hf_secret")

    for _ in range(3):  # once per recording
        appconfig.apply_ownscribe_config(Settings())

    assert list(cfg.parent.glob("config.toml.bak-*")) == []
    assert tomllib.loads(cfg.read_text(encoding="utf-8"))["diarization"]["hf_token"] == "hf_secret"
    assert cfg.stat().st_mode & 0o077 == 0
    assert cfg.parent.stat().st_mode & 0o077 == 0


def test_private_write_tightens_an_existing_readable_file(tmp_path):
    path = tmp_path / "secret"
    path.write_text("old")
    path.chmod(0o644)
    appconfig.write_private(path, b"new")
    assert path.read_bytes() == b"new"
    assert path.stat().st_mode & 0o777 == 0o600


def test_private_dir_is_owner_only(tmp_path):
    folder = tmp_path / "ownscribe"
    folder.mkdir(mode=0o755)
    assert appconfig.ensure_private_dir(folder) == folder
    assert folder.stat().st_mode & 0o777 == 0o700
    assert appconfig.ensure_private_dir(tmp_path / "new" / "dir").is_dir()


def test_launcher_names_the_tool_relative_to_home(tmp_path, monkeypatch):
    """/Applications is shared: each account must run its own copy of the tool."""
    from fly_transcriber import launcher

    home = tmp_path / "home"
    marker = tmp_path / "args"
    tool = home / ".local" / "bin" / "fly tool"
    tool.parent.mkdir(parents=True)
    tool.write_text(f'#!/bin/sh\necho "$@" > "{marker}"\n')
    tool.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))

    app = launcher.install_launcher((tmp_path / "Apps",), tool)
    stub = app / "Contents" / "MacOS" / "FLY"
    assert str(home) not in stub.read_text()
    assert '"$HOME"/' in stub.read_text()

    subprocess.run([str(stub)], check=True, env={"HOME": str(home), "PATH": "/usr/bin:/bin"})
    for _ in range(50):
        if marker.exists() and marker.read_text().strip():
            break
        time.sleep(0.1)
    assert marker.read_text().strip() == "--show"


# -- processing later ----------------------------------------------------------


def _wav(path, seconds, rate=48000, channels=1, bits=32, fmt=3, data_size=None):
    """A WAV header (float32 by default, as ownscribe's capture helper writes)."""
    import struct

    byte_rate = rate * channels * bits // 8
    data = b"\0" * int(byte_rate * seconds)
    size = len(data) if data_size is None else data_size
    fmt_chunk = struct.pack("<HHIIHH", fmt, channels, rate, byte_rate, channels * bits // 8, bits)
    path.write_bytes(
        b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
        + b"fmt " + struct.pack("<I", len(fmt_chunk)) + fmt_chunk
        + b"data" + struct.pack("<I", size) + data
    )
    return path


def test_audio_duration_reads_float_and_pcm_wavs(tmp_path):
    assert audio_duration(_wav(tmp_path / "f.wav", 2.0)) == pytest.approx(2.0)
    assert audio_duration(_wav(tmp_path / "p.wav", 1.5, bits=16, fmt=1, channels=2)) == pytest.approx(1.5)


def test_audio_duration_tolerates_unfinished_and_broken_files(tmp_path):
    # A capture cut short can leave the data size unwritten: use the file size.
    assert audio_duration(_wav(tmp_path / "u.wav", 1.0, data_size=0)) == pytest.approx(1.0)
    (tmp_path / "junk.wav").write_bytes(b"not a wav")
    assert audio_duration(tmp_path / "junk.wav") == 0.0
    assert audio_duration(tmp_path / "missing.wav") == 0.0


def test_a_recording_without_transcript_needs_processing(tmp_path):
    folder = tmp_path / "2026-11-04_1000"
    folder.mkdir()
    meeting = parse_meeting_dir(folder)
    assert not meeting.needs_processing  # nothing at all: not a pending recording
    _wav(folder / "recording.wav", 3.0)
    assert meeting.needs_processing
    assert meeting.duration == pytest.approx(3.0)
    (folder / "transcript.json").write_text('{"segments": []}', encoding="utf-8")
    assert not parse_meeting_dir(folder).needs_processing


def test_auto_process_is_off_by_default():
    assert Settings().auto_process is False


def test_recording_only_goes_through_the_launcher(tmp_path, monkeypatch):
    _ownscribe_script(tmp_path, monkeypatch)
    monkeypatch.setattr(recorder_mod, "has_mlx", lambda python: False)  # not needed to record
    cmd, env, _engine = Recorder(Settings()).record_command()
    assert cmd[:3] == [sys.executable, "-P", str(recorder_mod.MLX_LAUNCHER)]
    assert "--no-summarize" in cmd
    assert env["FLY_RECORD_ONLY"] == "1"


def test_auto_process_records_and_processes_in_one_run(tmp_path, monkeypatch):
    script = _ownscribe_script(tmp_path, monkeypatch)
    monkeypatch.setattr(recorder_mod, "has_mlx", lambda python: False)
    cmd, env, engine = Recorder(Settings(auto_process=True)).record_command()
    assert cmd[0] == str(script) and env is None and engine == "faster-whisper"


def test_without_ownscribes_python_recording_processes_at_once(monkeypatch):
    # The uvx fallback has no launcher to stop the pipeline after capture.
    monkeypatch.setattr(
        recorder_mod.shutil, "which", lambda name: "/bin/uvx" if name == "uvx" else None
    )
    cmd, env, _engine = Recorder(Settings()).record_command()
    assert cmd[:2] == ["/bin/uvx", "ownscribe"] and env is None


def test_processing_resumes_the_meeting_folder(tmp_path):
    folder = tmp_path / "2026-11-04_1000"
    args = Recorder(Settings(model="large-v3", language="fi")).process_args(folder)
    assert args == ["resume", str(folder.resolve()), "--model", "large-v3", "--language", "fi"]
    assert "--language" not in Recorder(Settings()).process_args(folder)


def test_processing_starts_in_the_transcribing_phase(tmp_path, monkeypatch):
    _ownscribe_script(tmp_path, monkeypatch)
    monkeypatch.setattr(recorder_mod, "has_mlx", lambda python: False)
    launched = []
    rec = Recorder(Settings())
    monkeypatch.setattr(rec, "_launch", lambda cmd, env: launched.append(cmd))
    folder = tmp_path / "2026-11-04_1000"
    rec.process(folder, audio_seconds=600)
    assert launched[0][1:3] == ["resume", str(folder.resolve())]
    assert rec.state.phase is Phase.TRANSCRIBING and rec.state.meeting_dir == folder
    assert rec.current_dir == folder
    assert rec.state.processing_eta > 0
    with pytest.raises(RuntimeError):
        rec.process(folder)  # one run at a time


def test_a_record_only_run_ends_as_saved(tmp_path):
    rec = Recorder(Settings(output_dir=str(tmp_path)))
    rec.state = RunState(phase=Phase.RECORDING)
    rec._finish(0)
    assert rec.state.phase is Phase.DONE and rec.state.detail == "Recording saved"


def test_defer_processing_stops_the_pipeline_after_capture(capsys):
    pipeline = types.SimpleNamespace(_do_transcribe_and_summarize=lambda *a: 1 / 0)
    mlx_launch.defer_processing(pipeline)
    pipeline._do_transcribe_and_summarize(None, Path("/x/recording.wav"), Path("/x"))
    assert mlx_launch.DEFERRED_MARKER in capsys.readouterr().out


def test_server_routes_process(tmp_path):
    asked = []
    api = Api(snapshot=lambda: {}, file_meeting=None, save_settings=None, forget=None,
              process=lambda name: asked.append(name) or {"ok": True})
    server = make_server(api, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/process", data=b'{"meeting": "2026-11-04_1000"}',
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            assert json.loads(resp.read()) == {"ok": True}
        assert asked == ["2026-11-04_1000"]
    finally:
        server.shutdown()


def test_a_failed_capture_is_not_waiting_to_be_processed(tmp_path):
    folder = tmp_path / "2026-11-04_2128"
    folder.mkdir()
    # What macOS refusing the capture leaves behind: the mic helper's temp file.
    _wav(folder / "recording.wav.mic.tmp.wav", 1.0)
    assert not parse_meeting_dir(folder).needs_processing
    _wav(folder / "recording.wav", 0.0)  # header only
    assert not parse_meeting_dir(folder).needs_processing


def test_failure_reason_explains_blocked_capture():
    log = (
        "  Recording: 00:00Error: Error Domain=com.apple.ScreenCaptureKit.SCStreamErrorDomain "
        'Code=-3801 "The user declined TCCs for application, window, display capture"\n'
        "Error: No audio was captured.\n"
    )
    assert "Screen & System Audio Recording" in recorder_mod.failure_reason(log)
    assert recorder_mod.failure_reason("Error: No audio was captured.\n") == "No audio was captured."
    assert recorder_mod.failure_reason("all fine") == ""


def test_silence_auto_stop_ends_the_recording_state():
    """ownscribe stops capture itself on the silence timeout; FLY must notice."""
    rec = Recorder(Settings())
    rec.state = RunState(phase=Phase.RECORDING)
    rec._interpret("  Recording: 05:00\n\nRecording auto-stopped after silence timeout.\n")
    assert rec.state.phase is Phase.SAVING
    assert not rec.state.is_recording and rec.state.is_active
    assert rec.state.detail == "Stopped after silence, saving"
    assert rec.state.processing_started_at is None  # nothing is being processed yet
    rec._interpret("Recording: 05:00 auto-stopped after silence timeout. Transcribing")
    assert rec.state.phase is Phase.TRANSCRIBING
    assert rec.state.processing_started_at is not None


def test_a_manual_stop_leaves_the_recording_state_at_once():
    rec = Recorder(Settings())
    rec.state = RunState(phase=Phase.RECORDING)
    rec._interpret("  Recording: 00:42\n\nStopping recording...\n")
    assert rec.state.phase is Phase.SAVING and rec.state.detail == "Saving recording"
