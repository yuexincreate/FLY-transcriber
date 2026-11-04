// Dashboard UI: Preact + htm, vendored so it runs offline with no build step.
import { html, render, useEffect, useMemo, useRef, useState } from "./vendor/htm-preact-3.1.1.js";
import {
  BRAND, BRAND_TEXT, Avatar, Button, Empty, Field, FilingFields, Icon, IconButton, Logo, Pill,
  RunBar, Switch, Toasts, api, awaitsFiling, fmtDuration, speakerHue, speakerName, status,
  useFilingForm, useToasts, PROCESS_BUSY_HINT, processMeeting, runBusy,
} from "./common.js";

const POLL_MS = 1000;

function useHashRoute() {
  const read = () => {
    const parts = location.hash.replace(/^#\/?/, "").split("/");
    if (parts[0] === "settings") return { view: "settings", adding: parts[1] === "add-project" };
    // From the popover: save a recording, optionally starting with project setup.
    if (parts[0] === "save" && parts[1]) {
      return { view: "save", name: decodeURIComponent(parts[1]), setup: parts[2] || null };
    }
    if (parts[0] === "r" && parts[1]) return { view: "recording", name: decodeURIComponent(parts[1]) };
    return { view: "recordings" };
  };
  const [route, setRoute] = useState(read);
  useEffect(() => {
    const onChange = () => { setRoute(read()); window.scrollTo(0, 0); };
    addEventListener("hashchange", onChange);
    return () => removeEventListener("hashchange", onChange);
  }, []);
  return route;
}

const go = (hash) => { location.hash = hash; };

async function copyTranscript(meeting, toast) {
  try {
    const detail = await api(`/api/meeting?name=${encodeURIComponent(meeting.name)}`);
    const title = meeting.title ? `# ${meeting.title}\n\n` : "";
    await navigator.clipboard.writeText(title + detail.markdown + "\n");
    toast("Transcript copied");
  } catch (err) {
    toast(err.message, "error");
  }
}

// -- chrome -----------------------------------------------------------------

const Footer = ({ version }) => html`
  <footer class="footer">
    <${Logo} size=${40} />
    <div class="footer-name">${BRAND.name}</div>
    <div class="footer-expansion">
      ${BRAND.expansion.map((w) => Array.isArray(w)
        ? html`<span><strong>${w[0]}</strong>${w[1]}</span>`
        : html`<span class="filler">${w}</span>`)}
    </div>
    ${version && html`<div class="footer-version">${version === "dev" ? "dev build" : `v${version}`}</div>`}
  </footer>`;

const RunStatus = ({ run }) => html`
  <div class=${`run run-${run.css || "idle"}`} title=${run.detail || ""}>
    <span class="run-dot"></span>
    <span class="run-label">${run.css === "idle" ? "Ready" : run.label}</span>
    ${run.detail && html`<span class="run-detail">${run.detail}</span>`}
    <${RunBar} progress=${run.progress} />
  </div>`;

// Start/stop in the top bar: the status pill becomes the button whenever the
// app can take one, and stays a plain status while processing.
const RecordButton = ({ run, onToggle }) => {
  const live = run.css === "recording";
  return html`
    <button type="button" class=${`run run-button ${live ? "run-recording" : "run-start"}`} onClick=${onToggle}
      title=${live ? "Stop recording and start transcribing" : "Start recording"}>
      <span class="run-dot"></span>
      <span class="run-label">${live ? "Stop" : "Start recording"}</span>
      ${live && html`<span class="run-elapsed">${run.elapsed}</span>`}
    </button>`;
};

const RunControl = ({ run, onToggle }) => {
  if (run.offline || run.css === "busy") return html`<${RunStatus} run=${run} />`;
  if (run.css === "failed") {
    return html`<div class="run-group"><${RunStatus} run=${run} /><${RecordButton} run=${run} onToggle=${onToggle} /></div>`;
  }
  return html`<${RecordButton} run=${run} onToggle=${onToggle} />`;
};

const TopBar = ({ route, run, onToggle }) => {
  const tab = route.view === "settings" ? "settings" : "recordings";
  return html`
    <header class="topbar">
      <div class="topbar-inner">
        <a class="brand" href="#/" title=${BRAND_TEXT} aria-label=${BRAND.name}>
          <${Logo} />
        </a>
        <nav class="nav" role="tablist">
          <a class=${`nav-item ${tab === "recordings" ? "active" : ""}`} href="#/"
            role="tab" aria-selected=${tab === "recordings"}><${Icon} name="mic" /> Recordings</a>
          <a class=${`nav-item ${tab === "settings" ? "active" : ""}`} href="#/settings"
            role="tab" aria-selected=${tab === "settings"}><${Icon} name="sliders" /> Settings</a>
        </nav>
        <${RunControl} run=${run} onToggle=${onToggle} />
      </div>
    </header>`;
};

// -- recordings list --------------------------------------------------------

/** Starts transcribing a recording that was saved without processing. */
const ProcessButton = ({ meeting, busy, toast, refresh, size, onClick }) => html`
  <${Button} size=${size} variant="primary" disabled=${busy}
    title=${busy ? PROCESS_BUSY_HINT : "Transcribe and label speakers"}
    onClick=${(e) => { onClick?.(e); processMeeting(meeting, toast, refresh); }}>Process<//>`;

const RecordingRow = ({ meeting, onFile, onDelete, busy, toast, refresh, selecting, selected, onSelect }) => {
  const st = status(meeting);
  const canAct = meeting.has_transcript && !meeting.processing;
  const meta = [meeting.when, meeting.duration ? fmtDuration(meeting.duration) : null];
  const n = meeting.speakers.length;
  if (n) meta.push(`${n} speaker${n === 1 ? "" : "s"}`);
  const open = () => go(`#/r/${encodeURIComponent(meeting.name)}`);
  const stop = (fn) => (e) => { e.stopPropagation(); fn(); };
  // A recording still being written or processed can't be deleted.
  const selectable = !meeting.processing;
  const toggle = () => selectable && onSelect(meeting.name);

  return html`
    <li class=${`row ${selecting ? "row-selecting" : ""} ${selected ? "row-selected" : ""}`}
      onClick=${selecting ? toggle : open}>
      ${selecting && html`
        <input type="checkbox" class="row-check" checked=${selected} disabled=${!selectable}
          aria-label=${`Select ${meeting.title || "Untitled recording"}${meeting.when ? `, ${meeting.when}` : ""}`}
          title=${selectable ? "" : "Still being processed"}
          onClick=${(e) => e.stopPropagation()} onChange=${toggle} />`}
      <div class="row-main">
        <div class="row-title">${meeting.title || "Untitled recording"}</div>
        <div class="row-meta">
          ${meta.filter(Boolean).join(" · ")}
          ${meeting.filed.length > 0 && html`
            <span class="row-filed">→ ${[...new Set(meeting.filed.map((f) => f.project))].join(", ")}</span>`}
        </div>
      </div>
      <${Pill} tone=${st.tone}>${st.label}<//>
      <div class="row-actions">
        ${!selecting && html`
        ${meeting.needs_processing && html`
          <${ProcessButton} size="sm" meeting=${meeting} busy=${busy} toast=${toast} refresh=${refresh}
            onClick=${(e) => e.stopPropagation()} />`}
        ${canAct && html`
          <${Button} size="sm" variant=${meeting.filed.length ? "secondary" : "primary"}
            onClick=${stop(() => onFile(meeting))}>${meeting.filed.length ? "Edit" : "Save"}<//>`}
        ${!meeting.processing && html`
          <${IconButton} icon="trash" label="Delete recording"
            onClick=${stop(() => onDelete([meeting]))} />`}
        <${IconButton} icon="chevron" label="Open" onClick=${stop(open)} />`}
      </div>
    </li>`;
};

const DeleteDialog = ({ meetings, onClose, onDeleted, toast }) => {
  const ref = useRef();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // Recordings still left to delete: after a partial failure, only those.
  const [left, setLeft] = useState(meetings);

  useEffect(() => { ref.current.showModal(); }, []);

  const remove = async () => {
    setBusy(true);
    setError("");
    const failed = [];
    let reason = "";
    // One at a time, through the same checks as a single delete.
    for (const m of left) {
      try {
        await api("/api/delete", { meeting: m.name });
      } catch (err) {
        failed.push(m);
        reason = err.message;
      }
    }
    const done = left.length - failed.length;
    if (done) toast(done === 1 ? "Moved to the Trash" : `${done} recordings moved to the Trash`);
    if (!failed.length) {
      onDeleted?.();
      ref.current.close();
      return;
    }
    setError(failed.length === 1 && left.length === 1
      ? reason
      : `${failed.length} could not be deleted: ${reason}`);
    setLeft(failed);
    setBusy(false);
  };

  const one = left.length === 1 ? left[0] : null;
  const filed = left.some((m) => m.filed.length);
  const audio = left.some((m) => m.has_audio);

  return html`
    <dialog class="dialog dialog-narrow" ref=${ref} onClose=${onClose}
      onClick=${(e) => e.target === ref.current && ref.current.close()}>
      <div class="dialog-body">
        <div class="dialog-head">
          <h2>${one ? "Delete this recording?" : `Delete ${left.length} recordings?`}</h2>
        </div>
        ${one
          ? html`<p>
              <strong>${one.title || "Untitled recording"}</strong>${one.when ? ` (${one.when})` : ""}${" "}
              and its transcript${one.has_audio ? " and audio" : ""} move to the Trash.
              ${filed ? " Notes already saved to your projects are kept." : ""}
            </p>`
          : html`<p>
              These recordings and their transcripts${audio ? " and audio" : ""} move to the Trash,
              where you can still restore them.${filed ? " Notes already saved to your projects are kept." : ""}
            </p>
            <ul class="delete-list">
              ${left.map((m) => html`<li key=${m.name}>
                <strong>${m.title || "Untitled recording"}</strong>${m.when ? ` · ${m.when}` : ""}
              </li>`)}
            </ul>`}
        ${error && html`<div class="error-text">${error}</div>`}
        <div class="dialog-foot">
          <${Button} onClick=${() => ref.current.close()}>Cancel<//>
          <${Button} variant="danger" disabled=${busy} onClick=${remove}>
            ${busy ? "Deleting…" : one ? "Delete" : `Delete ${left.length}`}
          <//>
        </div>
      </div>
    </dialog>`;
};

const RecordingsView = ({ meetings, onFile, toast, refresh, busy }) => {
  const unfiled = meetings.filter(awaitsFiling).length;
  const unprocessed = meetings.filter((m) => m.needs_processing).length;
  const waiting = [unprocessed && `${unprocessed} waiting to be processed`, unfiled && `${unfiled} waiting to be saved`]
    .filter(Boolean).join(", ");
  const [deleting, setDeleting] = useState(null);
  // Selection for batch delete, by meeting name so polling can't disturb it.
  const [selecting, setSelecting] = useState(false);
  const [picked, setPicked] = useState(() => new Set());
  const selectable = meetings.filter((m) => !m.processing);
  // Drop anything that disappeared or started processing since it was picked.
  const chosen = selectable.filter((m) => picked.has(m.name));
  const allPicked = selectable.length > 0 && chosen.length === selectable.length;
  const toggle = (name) => setPicked((prev) => {
    const next = new Set(prev);
    if (next.has(name)) next.delete(name);
    else next.add(name);
    return next;
  });
  const stopSelecting = () => { setSelecting(false); setPicked(new Set()); };
  useEffect(() => {
    if (!selecting) return;
    const onKey = (e) => { if (e.key === "Escape" && !deleting) stopSelecting(); };
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
  }, [selecting, deleting]);
  return html`
    <section class="page">
      <div class="page-head">
        <div>
          <h1>Recordings</h1>
          <p class="subtle">
            ${meetings.length
              ? waiting || "Everything is saved"
              : "Start a recording from the top right, or from the FLY icon in the menubar"}
          </p>
        </div>
        ${selectable.length > 0 && html`
          <div class="head-actions">
            ${selecting
              ? html`
                <${Button} size="sm" variant="ghost"
                  onClick=${() => setPicked(allPicked ? new Set() : new Set(selectable.map((m) => m.name)))}>
                  ${allPicked ? "Select none" : "Select all"}
                <//>
                <${Button} size="sm" onClick=${stopSelecting}>Done<//>`
              : html`<${Button} size="sm" onClick=${() => setSelecting(true)}>Select<//>`}
          </div>`}
      </div>
      ${meetings.length
        ? html`<ul class="list">
            ${meetings.map((m) => html`<${RecordingRow} key=${m.name} meeting=${m} onFile=${onFile}
              onDelete=${setDeleting} busy=${busy} toast=${toast} refresh=${refresh}
              selecting=${selecting} selected=${picked.has(m.name)} onSelect=${toggle} />`)}
          </ul>`
        : html`<${Empty} title="No recordings yet">
            Click Start recording at the top right, or the FLY icon in the menubar. Finished recordings show up here.
          <//>`}
      <div class=${`savebar ${selecting ? "visible" : ""}`} role="toolbar" aria-label="Selected recordings">
        <span>${chosen.length ? `${chosen.length} selected` : "Select recordings to delete"}</span>
        <${Button} variant="ghost" onClick=${stopSelecting}>Cancel<//>
        <${Button} variant="danger" icon="trash" disabled=${!chosen.length}
          onClick=${() => setDeleting(chosen)}>Delete${chosen.length ? ` ${chosen.length}` : ""}<//>
      </div>
      ${deleting && html`<${DeleteDialog} meetings=${deleting} toast=${toast}
        onDeleted=${() => { stopSelecting(); refresh?.(); }}
        onClose=${() => setDeleting(null)} />`}
    </section>`;
};

// -- single recording -------------------------------------------------------

function useDetail(meeting) {
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState("");
  // Refetch when anything that changes the rendered transcript changes.
  const key = meeting && JSON.stringify([meeting.name, meeting.has_transcript, meeting.state.speaker_names]);
  useEffect(() => {
    if (!meeting || !meeting.has_transcript) return;
    let live = true;
    api(`/api/meeting?name=${encodeURIComponent(meeting.name)}`)
      .then((d) => live && (setDetail(d), setError("")))
      .catch((e) => live && setError(e.message));
    return () => { live = false; };
  }, [key]);
  return [detail, error];
}

/** Consecutive turns by one speaker collapse into a single block. */
function groupTurns(turns) {
  const blocks = [];
  for (const turn of turns) {
    const last = blocks[blocks.length - 1];
    if (last && last.speaker === turn.speaker) last.lines.push(turn);
    else blocks.push({ speaker: turn.speaker, lines: [turn] });
  }
  return blocks;
}

/**
 * Shared state behind every pencil icon that renames a speaker: the sidebar
 * list and each turn in the transcript all write to the same label, so
 * editing from any one of them updates every line attributed to that person.
 */
function useSpeakerRename(meeting, label, toast, refresh) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState("");
  const [saving, setSaving] = useState(false);

  const start = () => { setValue(speakerName(label, meeting)); setEditing(true); };
  const cancel = () => setEditing(false);
  const save = async () => {
    setSaving(true);
    try {
      await api("/api/speaker-name", { meeting: meeting.name, speaker: label, name: value });
      await refresh();
      setEditing(false);
    } catch (err) {
      toast(err.message, "error");
    } finally {
      setSaving(false);
    }
  };
  const onKeyDown = (e) => { if (e.key === "Enter") save(); if (e.key === "Escape") cancel(); };

  return { editing, value, setValue, saving, start, cancel, save, onKeyDown };
}

/** The speaker name at the top of one transcript block, with its own pencil:
 * every occurrence of a person in the transcript can start the rename. */
const TurnSpeakerLabel = ({ label, meeting, toast, refresh }) => {
  const rename = useSpeakerRename(meeting, label, toast, refresh);
  const inputRef = useRef();
  useEffect(() => { if (rename.editing) inputRef.current?.select(); }, [rename.editing]);

  if (rename.editing) {
    return html`
      <span class="turn-speaker-group">
        <input class="input input-sm" ref=${inputRef} value=${rename.value} disabled=${rename.saving}
          onInput=${(e) => rename.setValue(e.currentTarget.value)} onKeyDown=${rename.onKeyDown} />
        <${IconButton} icon="check" label="Save name" disabled=${rename.saving} onClick=${rename.save} />
        <${IconButton} icon="close" label="Cancel" disabled=${rename.saving} onClick=${rename.cancel} />
      </span>`;
  }
  return html`
    <span class="turn-speaker-group">
      <span class="turn-speaker" data-hue=${speakerHue(label, meeting.speakers)}>${speakerName(label, meeting)}</span>
      <${IconButton} icon="pencil" label="Rename speaker" onClick=${rename.start} />
    </span>`;
};

const Transcript = ({ detail, meeting, toast, refresh }) => html`
  <div class="transcript">
    ${groupTurns(detail.turns).map((block, i) => html`
      <div class="turn" key=${i}>
        <${Avatar} label=${block.speaker} meeting=${meeting} />
        <div class="turn-body">
          <div class="turn-head">
            <${TurnSpeakerLabel} label=${block.speaker} meeting=${meeting} toast=${toast} refresh=${refresh} />
            <span class="turn-time">${block.lines[0].timestamp}</span>
          </div>
          ${block.lines.map((line) => html`<p key=${line.start} title=${line.timestamp}>${line.text}</p>`)}
        </div>
      </div>`)}
  </div>`;

/** One speaker in the sidebar list: a pencil turns the name into an inline field. */
const SpeakerListItem = ({ label, meeting, toast, refresh }) => {
  const rename = useSpeakerRename(meeting, label, toast, refresh);
  const inputRef = useRef();
  useEffect(() => { if (rename.editing) inputRef.current?.select(); }, [rename.editing]);

  if (rename.editing) {
    return html`
      <li>
        <${Avatar} label=${label} meeting=${meeting} />
        <input class="input input-sm" ref=${inputRef} value=${rename.value} disabled=${rename.saving}
          onInput=${(e) => rename.setValue(e.currentTarget.value)} onKeyDown=${rename.onKeyDown} />
        <${IconButton} icon="check" label="Save name" disabled=${rename.saving} onClick=${rename.save} />
        <${IconButton} icon="close" label="Cancel" disabled=${rename.saving} onClick=${rename.cancel} />
      </li>`;
  }
  return html`
    <li>
      <${Avatar} label=${label} meeting=${meeting} />
      <span class="speaker-name">${speakerName(label, meeting)}</span>
      <${IconButton} icon="pencil" label="Rename speaker" onClick=${rename.start} />
    </li>`;
};

const RecordingView = ({ meeting, onFile, toast, refresh, busy }) => {
  const [detail, error] = useDetail(meeting);
  const [deleting, setDeleting] = useState(false);
  if (!meeting) {
    return html`<section class="page">
      <a class="back" href="#/"><${Icon} name="back" /> Recordings</a>
      <${Empty} title="Recording not found">It may have been moved or deleted.<//>
    </section>`;
  }
  const st = status(meeting);
  const canAct = meeting.has_transcript && !meeting.processing;
  const reveal = () => api("/api/reveal", { meeting: meeting.name }).catch((e) => toast(e.message, "error"));

  return html`
    <section class="page">
      <a class="back" href="#/"><${Icon} name="back" /> Recordings</a>
      <div class="page-head">
        <div>
          <div class="title-line">
            <h1>${meeting.title || "Untitled recording"}</h1>
            <${Pill} tone=${st.tone}>${st.label}<//>
          </div>
          <p class="subtle">${[meeting.when, meeting.duration && fmtDuration(meeting.duration)].filter(Boolean).join(" · ")}</p>
        </div>
        <div class="head-actions">
          <${IconButton} icon="folder" label="Show in Finder" onClick=${reveal} />
          ${!meeting.processing && html`
            <${IconButton} icon="trash" label="Delete recording" onClick=${() => setDeleting(true)} />`}
          ${meeting.needs_processing && html`
            <${ProcessButton} meeting=${meeting} busy=${busy} toast=${toast} refresh=${refresh} />`}
          ${canAct && html`
            <${Button} icon="copy" onClick=${() => copyTranscript(meeting, toast)}>Copy<//>
            <${Button} variant="primary" onClick=${() => onFile(meeting)}>
              ${meeting.filed.length ? "Edit" : "Save"}
            <//>`}
        </div>
      </div>

      <div class="detail">
        <div class="card detail-main">
          ${meeting.processing
            ? html`<${Empty} title="Still processing">The transcript appears here when it is ready.<//>`
            : meeting.needs_processing
              ? html`<${Empty} title="Not processed yet">
                  The audio is saved. Press Process to transcribe it and label the speakers. It takes a few minutes, and you can record again once it has finished.
                <//>`
            : !meeting.has_transcript
              ? html`<${Empty} title="No transcript">This recording produced no transcript.<//>`
              : error
                ? html`<div class="error-text">${error}</div>`
                : detail
                  ? html`<${Transcript} detail=${detail} meeting=${meeting} toast=${toast} refresh=${refresh} />`
                  : html`<div class="loading">Loading transcript…</div>`}
        </div>

        <aside class="detail-side">
          <div class="card">
            <h3>Speakers</h3>
            ${meeting.speakers.length
              ? html`<ul class="speaker-list">
                  ${meeting.speakers.map((label) => html`
                    <${SpeakerListItem} key=${label} label=${label} meeting=${meeting} toast=${toast} refresh=${refresh} />`)}
                </ul>`
              : html`<p class="subtle">No speaker labels.</p>`}
          </div>
          <div class="card">
            <h3>Saved to</h3>
            ${meeting.filed.length
              ? html`<ul class="filed-list">
                  ${meeting.filed.map((f) => html`
                    <li key=${f.path}>
                      <span class="filed-project">${f.project}</span>
                      <code title=${f.path}>${f.path.split("/").pop()}</code>
                    </li>`)}
                </ul>`
              : html`<p class="subtle">Not saved yet.</p>`}
            <h3>Files</h3>
            <p class="subtle">${meeting.needs_processing ? "Audio waiting to be processed" : meeting.has_audio ? "Audio kept" : "Audio discarded"} · <code>${meeting.name}</code></p>
          </div>
        </aside>
      </div>
      ${deleting && html`<${DeleteDialog} meetings=${[meeting]} toast=${toast}
        onDeleted=${() => go("#/")} onClose=${() => setDeleting(false)} />`}
    </section>`;
};

// -- project setup --------------------------------------------------------

const basename = (path) => path.replace(/\/+$/, "").split("/").pop() || "";

const NAMING = [
  ["vault", "28-09-26-title.md"],
  ["timestamp", "2026-09-28_1420_title.md"],
];
const FRONTMATTER = [
  ["generic", "Generic"],
  ["obsidian", "Obsidian"],
];
const FRONTMATTER_HINTS = {
  generic: "ISO date, time, participants and speakers.",
  obsidian: "dd-mm-yy date, tags and a note for the agent.",
};

/** "Create a new project" or "Use an existing folder": the first question. */
const ProjectChoice = ({ onPick }) => html`
  <div class="stack">
    <p class="subtle">
      A project is a folder where FLY saves transcripts, for the AI agent working
      in that folder to turn into notes.
    </p>
    <div class="choices">
      <button type="button" class="choice" onClick=${() => onPick("new")}>
        <span class="choice-icon"><${Icon} name="plus" /></span>
        <span class="choice-title">Create a new project</span>
        <span class="choice-body">FLY makes a new folder for it, set up for an agent.</span>
      </button>
      <button type="button" class="choice" onClick=${() => onPick("existing")}>
        <span class="choice-icon"><${Icon} name="folder" /></span>
        <span class="choice-title">Use an existing folder</span>
        <span class="choice-body">An Obsidian vault, a repository, any folder you already work in.</span>
      </button>
    </div>
  </div>`;

/** The folder and its contents as they will be after adding, marked new or kept. */
const PlanPreview = ({ plan, kind }) => {
  const rel = (path) => path.startsWith(plan.root + "/") ? path.slice(plan.root.length + 1) : path;
  const inside = plan.steps.filter((s) => s.path !== plan.root);
  return html`
    <div class="plan">
      <div class="plan-title">What FLY will do</div>
      <ul class="plan-tree">
        <li class="plan-root">
          <${Icon} name="folder" />
          <code>${plan.root}</code>
          <span class=${`plan-tag ${kind === "new" ? "plan-new" : ""}`}>${kind === "new" ? "New folder" : "Your folder"}</span>
        </li>
        ${inside.map((step) => html`
          <li key=${step.path}>
            <${Icon} name=${step.kind} />
            <div>
              <code>${rel(step.path)}${step.kind === "folder" ? "/" : ""}</code>
              <div class="plan-purpose">${step.purpose}</div>
            </div>
            <span class=${`plan-tag ${step.exists ? "" : "plan-new"}`}>
              ${step.exists ? (step.kind === "file" ? "Exists, kept as is" : "Exists") : "New"}
            </span>
          </li>`)}
      </ul>
      ${plan.notes.map((n) => html`<p class="plan-note" key=${n}>${n}</p>`)}
      <p class="field-hint">
        FLY lists <strong>${plan.name}</strong> as a place to save to. Nothing else in the
        folder is read or changed, and removing the project from FLY later leaves every file in place.
      </p>
    </div>`;
};

const FolderField = ({ label, hint, value, onChange, prompt }) => {
  const [busy, setBusy] = useState(false);
  const choose = async () => {
    setBusy(true);
    try {
      const { path } = await api("/api/choose-folder", { prompt, default: value });
      if (path) onChange(path);
    } catch { /* cancelled, or no picker: the path can still be typed */ }
    setBusy(false);
  };
  return html`
    <${Field} label=${label} hint=${hint}>
      <div class="folder-input">
        <input class="input" value=${value} placeholder="~/Documents/Acme" spellcheck="false"
          onInput=${(e) => onChange(e.currentTarget.value)} />
        <${Button} onClick=${choose} disabled=${busy}>${busy ? "Choosing…" : "Choose…"}<//>
      </div>
    <//>`;
};

/**
 * Setting up a destination. Every change asks the server for the plan, so what
 * the form says will happen is exactly what adding does.
 */
const ProjectSetup = ({ kind, onBack, onDone, toast }) => {
  const isNew = kind === "new";
  const [name, setName] = useState("");
  const [nameTouched, setNameTouched] = useState(false);
  const [folder, setFolder] = useState(isNew ? "~" : "");
  const [inbox, setInbox] = useState("meetings/_inbox");
  const [agentFiles, setAgentFiles] = useState(true);
  const [naming, setNaming] = useState("vault");
  const [frontmatter, setFrontmatter] = useState("generic");
  const [plan, setPlan] = useState(null);
  const [problem, setProblem] = useState("");
  const [busy, setBusy] = useState(false);
  const ref = useRef();

  // An existing folder names the project until the user names it themselves.
  const effectiveName = nameTouched || isNew ? name : basename(folder);
  const request = {
    kind, name: effectiveName, folder, inbox, agent_files: agentFiles, naming, frontmatter,
  };
  const key = JSON.stringify(request);

  useEffect(() => { ref.current?.querySelector("input")?.focus(); }, []);
  useEffect(() => {
    let live = true;
    const timer = setTimeout(() => {
      api("/api/project/plan", request)
        .then((p) => live && (setPlan(p), setProblem("")))
        .catch((e) => live && (setPlan(null), setProblem(e.message)));
    }, 200);
    return () => { live = false; clearTimeout(timer); };
  }, [key]);

  const add = async () => {
    setBusy(true);
    try {
      const result = await api("/api/project/add", request);
      toast(`${result.name} added`);
      onDone(result.name);
    } catch (err) {
      setProblem(err.message);
      setBusy(false);
    }
  };

  const nameField = html`
    <${Field} label="Project name" hint=${isNew ? "Also names the folder." : "Shown when saving. Defaults to the folder's name."}>
      <input class="input" value=${effectiveName} placeholder="Acme"
        onInput=${(e) => { setName(e.currentTarget.value); setNameTouched(true); }} />
    <//>`;
  const folderField = html`
    <${FolderField} value=${folder} onChange=${setFolder}
      label=${isNew ? "Create it in" : "Folder"}
      hint=${isNew ? "The project folder is made inside this one." : "The project's top folder, where its agent works."}
      prompt=${isNew ? "Where should the new project folder go?" : "Choose the project folder"} />`;

  return html`
    <div class="stack" ref=${ref}>
      ${isNew ? [nameField, folderField] : [folderField, nameField]}

      <details class="options">
        <summary>Options</summary>
        <div class="stack">
          <${Field} label="Transcripts folder" hint="Inside the project. Leave empty to save into the project folder itself.">
            <input class="input" value=${inbox} placeholder="meetings/_inbox" spellcheck="false"
              onInput=${(e) => setInbox(e.currentTarget.value)} />
          <//>
          <${Switch} label="Add agent instructions"
            hint="CLAUDE.md and the meeting skill, so an agent knows what to do with the transcripts. Existing files are never overwritten."
            checked=${agentFiles} onChange=${setAgentFiles} />
          <div class="grid-2">
            <${Field} label="File names" hint=${naming === "vault" ? "Day first, like a vault note." : "Sorts by recording time."}>
              <select class="input" value=${naming} onChange=${(e) => setNaming(e.currentTarget.value)}>
                ${NAMING.map(([v, l]) => html`<option key=${v} value=${v}>${l}</option>`)}
              </select>
            <//>
            <${Field} label="Frontmatter" hint=${FRONTMATTER_HINTS[frontmatter]}>
              <select class="input" value=${frontmatter} onChange=${(e) => setFrontmatter(e.currentTarget.value)}>
                ${FRONTMATTER.map(([v, l]) => html`<option key=${v} value=${v}>${l}</option>`)}
              </select>
            <//>
          </div>
        </div>
      </details>

      ${plan
        ? html`<${PlanPreview} plan=${plan} kind=${kind} />`
        : problem && html`<div class="plan plan-problem">${problem}</div>`}

      <div class="dialog-foot">
        <${Button} variant="ghost" class="foot-start" icon="back" onClick=${onBack}>Back<//>
        <${Button} variant="primary" disabled=${!plan || busy} onClick=${add}>
          ${busy ? "Adding…" : isNew ? "Create project" : "Add project"}
        <//>
      </div>
    </div>`;
};

const SETUP_TITLES = { choose: "Add a project", new: "Create a new project", existing: "Use an existing folder" };

/** The choice, then the form for it. ``step`` is "choose", "new" or "existing". */
const ProjectSteps = ({ step, setStep, onBack, onDone, toast }) =>
  step === "choose"
    ? html`
        <${ProjectChoice} onPick=${setStep} />
        ${onBack && html`<div class="dialog-foot">
          <${Button} variant="ghost" class="foot-start" icon="back" onClick=${onBack}>Back<//>
        </div>`}`
    : html`<${ProjectSetup} key=${step} kind=${step} toast=${toast} onDone=${onDone}
        onBack=${() => setStep("choose")} />`;

const AddProjectDialog = ({ onClose, onAdded, toast }) => {
  const ref = useRef();
  const [step, setStep] = useState("choose");
  useEffect(() => { ref.current.showModal(); }, []);
  return html`
    <dialog class="dialog" ref=${ref} onClose=${onClose}
      onClick=${(e) => e.target === ref.current && ref.current.close()}>
      <div class="dialog-body">
        <div class="dialog-head">
          <h2>${SETUP_TITLES[step]}</h2>
          <${IconButton} icon="close" label="Close" onClick=${() => ref.current.close()} />
        </div>
        <${ProjectSteps} step=${step} setStep=${setStep} toast=${toast}
          onDone=${() => { onAdded(); ref.current.close(); }} />
      </div>
    </dialog>`;
};

const RemoveProjectDialog = ({ project, onClose, onRemoved, toast }) => {
  const ref = useRef();
  const [error, setError] = useState("");
  useEffect(() => { ref.current.showModal(); }, []);
  const remove = async () => {
    try {
      await api("/api/project/remove", { name: project.name });
      toast(`${project.name} removed from FLY`);
      onRemoved();
      ref.current.close();
    } catch (err) {
      setError(err.message);
    }
  };
  return html`
    <dialog class="dialog dialog-narrow" ref=${ref} onClose=${onClose}
      onClick=${(e) => e.target === ref.current && ref.current.close()}>
      <div class="dialog-body">
        <div class="dialog-head"><h2>Remove ${project.name} from FLY?</h2></div>
        <p>FLY stops offering it when saving. Nothing is deleted: <code>${project.display}</code>${" "}
          and everything in it, including transcripts already saved there, stay where they are.</p>
        ${error && html`<div class="error-text">${error}</div>`}
        <div class="dialog-foot">
          <${Button} onClick=${() => ref.current.close()}>Cancel<//>
          <${Button} variant="danger" onClick=${remove}>Remove<//>
        </div>
      </div>
    </dialog>`;
};

// -- filing dialog ----------------------------------------------------------

const FileDialog = ({ meeting, projects, setup: initialSetup, onClose, refresh, toast }) => {
  const ref = useRef();
  // null = the save form; otherwise a project setup step shown in its place.
  const [setup, setSetup] = useState(initialSetup);
  const form = useFilingForm(meeting, projects, {
    onFiled: (project) => { toast(`Saved to ${project}`); ref.current.close(); },
    onSkipped: () => { toast("Skipped — it won't count as waiting"); ref.current.close(); },
  });

  useEffect(() => {
    ref.current.showModal();
    ref.current.querySelector("input")?.focus();
  }, []);

  const added = async (name) => {
    await refresh();
    form.setProject(name);
    setSetup(null);
  };

  return html`
    <dialog class="dialog" ref=${ref} onClose=${onClose}
      onClick=${(e) => e.target === ref.current && ref.current.close()}>
      ${setup
        ? html`<div class="dialog-body">
            <div class="dialog-head">
              <div>
                <h2>${SETUP_TITLES[setup]}</h2>
                <p class="subtle">Then save ${meeting.title ? `“${meeting.title}”` : "the recording"} into it.</p>
              </div>
              <${IconButton} icon="close" label="Close" onClick=${() => ref.current.close()} />
            </div>
            <${ProjectSteps} step=${setup} setStep=${setSetup} toast=${toast} onDone=${added}
              onBack=${() => setSetup(null)} />
          </div>`
        : html`<form method="dialog" class="dialog-body" onSubmit=${form.submit}>
        <div class="dialog-head">
          <div>
            <h2>${meeting.filed.length ? "Edit saved note" : "Save recording"}</h2>
            <p class="subtle">${meeting.when}${meeting.duration ? ` · ${fmtDuration(meeting.duration)}` : ""}</p>
          </div>
          <${IconButton} icon="close" label="Close" onClick=${() => ref.current.close()} />
        </div>

        <${FilingFields} form=${form} meeting=${meeting} projects=${projects}
          onAddProject=${(kind) => setSetup(kind || "choose")} />

        <div class="dialog-foot">
          ${awaitsFiling(meeting) && html`
            <${Button} variant="ghost" class="foot-start" onClick=${form.skip}
              title="Stop counting this one as waiting to be saved">Don't save<//>`}
          <${Button} onClick=${() => ref.current.close()}>Cancel<//>
          <button type="submit" class="btn btn-primary" disabled=${form.busy || !projects.length}>
            ${form.busy ? "Saving…" : `${meeting.filed.length ? "Save changes to" : "Save to"} ${form.project || "…"}`}
          </button>
        </div>
      </form>`}
    </dialog>`;
};

// -- settings ---------------------------------------------------------------

/** Whisper's languages, by the code ownscribe expects. "" is auto-detect. */
const LANGUAGES = [
  ["", "Auto-detect"],
  ...[
    ["af", "Afrikaans"], ["sq", "Albanian"], ["am", "Amharic"], ["ar", "Arabic"], ["hy", "Armenian"],
    ["as", "Assamese"], ["az", "Azerbaijani"], ["ba", "Bashkir"], ["eu", "Basque"], ["be", "Belarusian"],
    ["bn", "Bengali"], ["bs", "Bosnian"], ["br", "Breton"], ["bg", "Bulgarian"], ["my", "Burmese"],
    ["yue", "Cantonese"], ["ca", "Catalan"], ["zh", "Chinese"], ["hr", "Croatian"], ["cs", "Czech"],
    ["da", "Danish"], ["nl", "Dutch"], ["en", "English"], ["et", "Estonian"], ["fo", "Faroese"],
    ["fi", "Finnish"], ["fr", "French"], ["gl", "Galician"], ["ka", "Georgian"], ["de", "German"],
    ["el", "Greek"], ["gu", "Gujarati"], ["ht", "Haitian Creole"], ["ha", "Hausa"], ["haw", "Hawaiian"],
    ["he", "Hebrew"], ["hi", "Hindi"], ["hu", "Hungarian"], ["is", "Icelandic"], ["id", "Indonesian"],
    ["it", "Italian"], ["ja", "Japanese"], ["jw", "Javanese"], ["kn", "Kannada"], ["kk", "Kazakh"],
    ["km", "Khmer"], ["ko", "Korean"], ["lo", "Lao"], ["la", "Latin"], ["lv", "Latvian"],
    ["ln", "Lingala"], ["lt", "Lithuanian"], ["lb", "Luxembourgish"], ["mk", "Macedonian"],
    ["mg", "Malagasy"], ["ms", "Malay"], ["ml", "Malayalam"], ["mt", "Maltese"], ["mi", "Maori"],
    ["mr", "Marathi"], ["mn", "Mongolian"], ["ne", "Nepali"], ["no", "Norwegian"], ["nn", "Norwegian Nynorsk"],
    ["oc", "Occitan"], ["ps", "Pashto"], ["fa", "Persian"], ["pl", "Polish"], ["pt", "Portuguese"],
    ["pa", "Punjabi"], ["ro", "Romanian"], ["ru", "Russian"], ["sa", "Sanskrit"], ["sr", "Serbian"],
    ["sn", "Shona"], ["sd", "Sindhi"], ["si", "Sinhala"], ["sk", "Slovak"], ["sl", "Slovenian"],
    ["so", "Somali"], ["es", "Spanish"], ["su", "Sundanese"], ["sw", "Swahili"], ["sv", "Swedish"],
    ["tl", "Tagalog"], ["tg", "Tajik"], ["ta", "Tamil"], ["tt", "Tatar"], ["te", "Telugu"],
    ["th", "Thai"], ["bo", "Tibetan"], ["tr", "Turkish"], ["tk", "Turkmen"], ["uk", "Ukrainian"],
    ["ur", "Urdu"], ["uz", "Uzbek"], ["vi", "Vietnamese"], ["cy", "Welsh"], ["yi", "Yiddish"],
    ["yo", "Yoruba"],
  ],
];

const languageName = (code) => LANGUAGES.find(([c]) => c === code)?.[1] || code;

/**
 * Type to filter by name or code, pick with the mouse or arrows + Enter. Shows
 * the chosen language's name; leaving without picking keeps the old choice.
 */
const LanguagePicker = ({ value, onChange }) => {
  const [query, setQuery] = useState(null); // null = closed, showing the selection
  const [active, setActive] = useState(0);
  const listRef = useRef();
  const q = (query || "").trim().toLowerCase();
  const matches = query === null ? [] : LANGUAGES.filter(([code, name]) =>
    !q || name.toLowerCase().includes(q) || code === q);

  useEffect(() => {
    listRef.current?.children[active]?.scrollIntoView({ block: "nearest" });
  }, [active, query]);

  const open = () => {
    setQuery("");
    setActive(Math.max(0, LANGUAGES.findIndex(([c]) => c === value)));
  };
  const pick = ([code]) => { onChange(code); setQuery(null); };
  const onKey = (e) => {
    if (query === null) {
      if (e.key === "ArrowDown" || e.key === "Enter") { e.preventDefault(); open(); }
      return;
    }
    if (e.key === "ArrowDown") { e.preventDefault(); setActive((i) => Math.min(i + 1, matches.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive((i) => Math.max(i - 1, 0)); }
    else if (e.key === "Enter") { e.preventDefault(); if (matches[active]) pick(matches[active]); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); setQuery(null); }
  };

  return html`
    <div class="combo">
      <input class="input combo-input" role="combobox" aria-expanded=${query !== null}
        aria-autocomplete="list" autocomplete="off" spellcheck="false"
        value=${query ?? languageName(value)} placeholder=${languageName(value)}
        onFocus=${open} onClick=${() => query === null && open()} onBlur=${() => setQuery(null)}
        onInput=${(e) => { setQuery(e.currentTarget.value); setActive(0); }} onKeyDown=${onKey} />
      ${query !== null && html`
        <ul class="combo-list" role="listbox" ref=${listRef}>
          ${matches.length
            ? matches.map((lang, i) => html`
                <li key=${lang[0]} role="option" aria-selected=${lang[0] === value}
                  class=${`combo-option ${i === active ? "active" : ""}`}
                  onMouseDown=${(e) => { e.preventDefault(); pick(lang); }}
                  onClick=${(e) => e.preventDefault() /* no label activation reopening it */}
                  onMouseEnter=${() => setActive(i)}>
                  <span>${lang[1]}</span>
                  ${lang[0] && html`<span class="combo-code">${lang[0]}</span>`}
                </li>`)
            : html`<li class="combo-empty">No matching language</li>`}
        </ul>`}
    </div>`;
};

const MODELS = [
  ["large-v3", "large-v3 — recommended"],
  ["medium", "medium"],
  ["small", "small"],
  ["base", "base"],
  ["tiny", "tiny — fastest, least accurate"],
];

const ProjectsCard = ({ projects, adding, refresh, toast }) => {
  const [removing, setRemoving] = useState(null);
  const reveal = (p) => api("/api/project/reveal", { name: p.name }).catch((e) => toast(e.message, "error"));
  return html`
    <div class="card section">
      <div class="section-head">
        <div>
          <h2>Projects</h2>
          <p class="subtle">Folders FLY can save transcripts into.</p>
        </div>
        <${Button} icon="plus" onClick=${() => go("#/settings/add-project")}>Add project<//>
      </div>
      ${projects.length
        ? html`<ul class="project-list">
            ${projects.map((p) => html`
              <li key=${p.name}>
                <div class="project-main">
                  <div class="row-title">${p.name}</div>
                  <div class="row-meta"><code title=${p.path}>${p.display || p.path}</code></div>
                  <div class="row-meta">${p.naming} file names · ${p.frontmatter} frontmatter</div>
                </div>
                <div class="row-actions">
                  <${IconButton} icon="folder" label="Show in Finder" onClick=${() => reveal(p)} />
                  <${IconButton} icon="trash" label=${`Remove ${p.name} from FLY`} onClick=${() => setRemoving(p)} />
                </div>
              </li>`)}
          </ul>`
        : html`<p class="subtle">No projects yet. Add one to start saving transcripts.</p>`}
      ${adding && html`<${AddProjectDialog} toast=${toast} onAdded=${refresh}
        onClose=${() => go("#/settings")} />`}
      ${removing && html`<${RemoveProjectDialog} project=${removing} toast=${toast}
        onRemoved=${refresh} onClose=${() => setRemoving(null)} />`}
    </div>`;
};

const UpdateDialog = ({ latest, onClose, onApplied }) => {
  const ref = useRef();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => { ref.current.showModal(); }, []);

  const update = async () => {
    setBusy(true);
    setError("");
    try {
      await api("/api/update/apply", {});
      onApplied();
      ref.current.close();
    } catch (err) {
      setError(err.message);
      setBusy(false);
    }
  };

  return html`
    <dialog class="dialog dialog-narrow" ref=${ref} onClose=${onClose}
      onClick=${(e) => e.target === ref.current && ref.current.close()}>
      <div class="dialog-body">
        <div class="dialog-head">
          <h2>Update to v${latest}?</h2>
        </div>
        <p>
          FLY will quit and reopen on its own once the update is installed.
          This can take a minute or two — keep the internet connection on and
          don't start a recording until it's back.
        </p>
        ${error && html`<div class="error-text">${error}</div>`}
        <div class="dialog-foot">
          <${Button} onClick=${() => ref.current.close()}>Cancel<//>
          <${Button} variant="primary" disabled=${busy} onClick=${update}>${busy ? "Updating…" : "Update"}<//>
        </div>
      </div>
    </dialog>`;
};

const UpdateCard = ({ version, update, toast }) => {
  const [checking, setChecking] = useState(false);
  const [info, setInfo] = useState(null);
  const [confirming, setConfirming] = useState(false);
  // The running version at the moment the update was triggered; once the app
  // comes back on a different version, the update is done.
  const [applied, setApplied] = useState(null);
  const failed = update?.status === "failed";
  // Waiting for the new version; a failed run hands the buttons back.
  const pending = applied && !failed;

  useEffect(() => {
    if (applied && version && version !== applied) {
      toast(`Updated to v${version}`);
      setApplied(null);
      setInfo(null);
    }
  }, [version]);

  const check = async () => {
    setChecking(true);
    try {
      setInfo(await api("/api/update/check", {}));
    } catch (err) {
      toast(err.message, "error");
    } finally {
      setChecking(false);
    }
  };

  return html`
    <div class="card section">
      <div class="section-head">
        <div>
          <h2>Updates</h2>
          <p class="subtle">Running v${version || "dev"}</p>
        </div>
        ${!pending && html`
          ${info?.available
            ? html`<${Button} variant="primary" onClick=${() => setConfirming(true)}>Update to v${info.latest}<//>`
            : html`<${Button} icon="refresh" onClick=${check} disabled=${checking}>
                ${checking ? "Checking…" : "Check for updates"}<//>`}`}
      </div>
      ${pending && html`<p class="subtle">
        Updating in the background — FLY will quit and reopen on its own. This can take a few minutes.
      </p>`}
      ${failed && html`
        <div class="update-failed">
          <div class="error-text">The update didn't finish: ${update.error}</div>
          <p class="subtle">
            Run it in Terminal instead, where you can see each step. It quits FLY, installs the new
            version and opens it again:
          </p>
          <div class="update-command">
            <code>${update.command}</code>
            <${Button} size="sm" icon="copy" onClick=${() =>
              navigator.clipboard.writeText(update.command).then(() => toast("Copied"), () => toast("Couldn't copy", "error"))}>Copy<//>
          </div>
          <p class="subtle">The full output is in <code>${update.log}</code>.</p>
        </div>`}
      ${!pending && !failed && info && !info.available && !info.error && html`<p class="subtle">You're up to date.</p>`}
      ${!pending && info?.error && html`<div class="error-text">${info.error}</div>`}
      ${confirming && html`<${UpdateDialog} latest=${info.latest}
        onApplied=${() => setApplied(version)} onClose=${() => setConfirming(false)} />`}
    </div>`;
};

const SettingsView = ({ settings, projects, adding, refresh, toast, version, update }) => {
  const [draft, setDraft] = useState(settings);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const serverKey = JSON.stringify(settings);

  // Follow the server until the user starts editing.
  useEffect(() => { if (!dirty) setDraft(settings); }, [serverKey]);

  const set = (key) => (value) => { setDraft((d) => ({ ...d, [key]: value })); setDirty(true); };
  const text = (key) => (e) => set(key)(e.currentTarget.value);
  const num = (key) => (e) => set(key)(Number(e.currentTarget.value));

  const save = async (event) => {
    event.preventDefault();
    setSaving(true);
    try {
      await api("/api/settings", draft);
      setDirty(false);
      toast("Saved — applies to the next recording");
    } catch (err) {
      toast(err.message, "error");
    } finally {
      setSaving(false);
    }
  };

  if (!draft) return null;
  return html`
    <form class="page" onSubmit=${save}>
      <div class="page-head">
        <div>
          <h1>Settings</h1>
          <p class="subtle">Changes apply from the next recording.</p>
        </div>
      </div>

      <div class="card section">
        <h2>Transcription</h2>
        <div class="grid-2">
          <${Field} label="Whisper model">
            <select class="input" value=${draft.model} onChange=${text("model")}>
              ${MODELS.map(([v, l]) => html`<option key=${v} value=${v}>${l}</option>`)}
            </select>
          <//>
          <${Field} label="Language" hint="Setting it helps when auto-detect guesses wrong.">
            <${LanguagePicker} value=${draft.language} onChange=${set("language")} />
          <//>
        </div>
      </div>

      <div class="card section">
        <h2>Speakers</h2>
        <${Switch} label="Identify speakers" hint="Diarization with pyannote, using the speaker model the installer set up."
          checked=${draft.diarize} onChange=${set("diarize")} />
        <div class="grid-2">
          <${Field} label="Speaker count"
            hint="0 = auto. Over-stating it makes diarization split one person in two, convincingly.">
            <input class="input" type="number" min="0" max="20" value=${draft.speaker_count} onInput=${num("speaker_count")} />
          <//>
        </div>
      </div>

      <div class="card section">
        <h2>Recording</h2>
        <${Switch} label="Capture microphone" hint="In addition to system audio."
          checked=${draft.mic} onChange=${set("mic")} />
        <${Switch} label="Process recordings automatically"
          hint="Off: stopping saves the recording, and you press Process when you want the transcript. On: processing starts as soon as you stop."
          checked=${draft.auto_process} onChange=${set("auto_process")} />
        <${Switch} label="Keep audio files" hint="About 300 MB per hour. Needed to re-run diarization later."
          checked=${draft.keep_recording} onChange=${set("keep_recording")} />
        <div class="grid-2">
          <${Field} label="Silence auto-stop" hint="Seconds of silence before stopping. 0 disables it.">
            <input class="input" type="number" min="0" step="30" value=${draft.silence_timeout} onInput=${num("silence_timeout")} />
          <//>
        </div>
      </div>

      <${ProjectsCard} projects=${projects} adding=${adding} refresh=${refresh} toast=${toast} />

      <${UpdateCard} version=${version} update=${update} toast=${toast} />

      <div class=${`savebar ${dirty ? "visible" : ""}`}>
        <span>Unsaved changes</span>
        <${Button} onClick=${() => { setDraft(settings); setDirty(false); }}>Discard<//>
        <button type="submit" class="btn btn-primary" disabled=${saving}>${saving ? "Saving…" : "Save"}</button>
      </div>
    </form>`;
};

// -- app --------------------------------------------------------------------

const DISCONNECTED = { css: "failed", label: "Disconnected", detail: "Is the app running?", offline: true };

function App() {
  const route = useHashRoute();
  const [state, setState] = useState(null);
  const [run, setRun] = useState({ css: "idle", label: "Connecting…" });
  const [filing, setFiling] = useState(null);
  const [toasts, toast] = useToasts();

  const refresh = async () => {
    try {
      const next = await api("/api/state");
      setState(next);
      setRun(next.run);
    } catch {
      setRun(DISCONNECTED);
    }
  };

  const toggleRecording = () => api("/api/record", {}).then(refresh).catch((e) => toast(e.message, "error"));

  useEffect(() => {
    let timer;
    const poll = async () => { await refresh(); timer = setTimeout(poll, POLL_MS); };
    poll();
    return () => clearTimeout(timer);
  }, []);

  // The popover hands saving over here when a project has to be set up first.
  const [setup, setSetup] = useState(null);
  useEffect(() => {
    if (route.view !== "save") return;
    setFiling(route.name);
    setSetup(route.setup === "add" ? "choose" : route.setup);
  }, [route.view, route.name, route.setup]);
  const closeFiling = () => {
    setFiling(null);
    setSetup(null);
    if (route.view === "save") go("#/");
  };

  const meetings = state?.meetings || [];
  const projects = state?.projects || [];
  const byName = useMemo(() => Object.fromEntries(meetings.map((m) => [m.name, m])), [meetings]);
  // Look the meeting up fresh on each poll, so the dialog never files stale data.
  const filingMeeting = filing && byName[filing];

  // Opening the dashboard with a recording waiting goes straight to filing it.
  // Only on the first load: later polls must not reopen a dialog just closed.
  const autoOpened = useRef(false);
  useEffect(() => {
    if (!state || autoOpened.current) return;
    autoOpened.current = true;
    const waiting = meetings.find(awaitsFiling); // newest first
    if (waiting && route.view === "recordings") setFiling(waiting.name);
  }, [state]);

  let page;
  if (!state) page = html`<div class="loading page">Loading…</div>`;
  else if (route.view === "settings") {
    page = html`<${SettingsView} settings=${state.settings} projects=${projects}
      adding=${route.adding} refresh=${refresh} toast=${toast} version=${state.version}
      update=${state.update} />`;
  }
  else if (route.view === "recording") page = html`<${RecordingView} meeting=${byName[route.name]} onFile=${(m) => setFiling(m.name)} toast=${toast} refresh=${refresh} busy=${runBusy(run)} />`;
  else page = html`<${RecordingsView} meetings=${meetings} onFile=${(m) => setFiling(m.name)} toast=${toast} refresh=${refresh} busy=${runBusy(run)} />`;

  return html`
    <${TopBar} route=${route} run=${run} onToggle=${toggleRecording} />
    <main>${page}</main>
    <${Footer} version=${state?.version} />
    ${filingMeeting && html`<${FileDialog} key=${`${filing}/${setup}`} meeting=${filingMeeting}
      projects=${projects} setup=${setup} refresh=${refresh} toast=${toast} onClose=${closeFiling} />`}
    <${Toasts} toasts=${toasts} />`;
}

document.title = `${BRAND.name} — ${BRAND_TEXT}`;
render(html`<${App} />`, document.getElementById("root"));
