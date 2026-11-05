// Shared by the dashboard (app.js) and the menubar popover (popover.js).
import { html, useCallback, useState } from "./vendor/htm-preact-3.1.1.js";

const SPEAKER_HUES = 6;

export const BRAND = { name: "FLY" };


// -- helpers ----------------------------------------------------------------

export function fmtDuration(seconds) {
  const s = Math.max(0, Math.floor(seconds || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h) return `${h} h ${m} min`;
  if (m) return `${m} min`;
  return `${s} s`;
}

export async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (data.error) throw new Error(data.error);
  return data;
}


/** Stable colour slot per diarization label, so a speaker keeps one colour everywhere. */
export function speakerHue(label, speakers) {
  const index = speakers.indexOf(label);
  return index < 0 ? "none" : String(index % SPEAKER_HUES);
}

/** "SPEAKER_01" reads as "Speaker 2" until the user names them. */
export function prettyLabel(label) {
  const match = label?.match(/^SPEAKER_(\d+)$/);
  return match ? `Speaker ${Number(match[1]) + 1}` : label || "Unattributed";
}

export function speakerName(label, meeting) {
  return meeting.state.speaker_names?.[label] || prettyLabel(label);
}

export function initials(name) {
  const match = name.match(/^(?:SPEAKER_|Speaker )(\d+)$/);
  if (match) return name.startsWith("SPEAKER_") ? String(Number(match[1]) + 1) : match[1];
  return name.split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase() || "?";
}

export function status(meeting) {
  if (meeting.processing) return { tone: "busy", label: "Processing" };
  if (meeting.needs_processing) return { tone: "warn", label: "Not processed" };
  if (!meeting.has_transcript) return { tone: "muted", label: "No transcript" };
  if (meeting.filed.length) return { tone: "ok", label: "Saved" };
  if (meeting.state.dismissed) return { tone: "muted", label: "Skipped" };
  return { tone: "warn", label: "Not saved" };
}

/** Whether a recording or processing run is going on (only one runs at a time). */
export const runBusy = (run) => run?.css === "recording" || run?.css === "busy";

/** Start transcribing a recording that was saved without processing. */
export const processMeeting = (meeting, toast, refresh) =>
  api("/api/process", { meeting: meeting.name })
    .then(() => refresh?.())
    .catch((e) => toast(e.message, "error"));

export const PROCESS_BUSY_HINT = "Available when the current recording has finished";

/** Matches the menubar's "Ready to save (n)" count. */
export const awaitsFiling = (m) =>
  m.has_transcript && !m.processing && !m.filed.length && !m.state.dismissed;


// -- icons ------------------------------------------------------------------

export const ICONS = {
  copy: "M8 8V5.5A1.5 1.5 0 0 1 9.5 4h9A1.5 1.5 0 0 1 20 5.5v9a1.5 1.5 0 0 1-1.5 1.5H16M5.5 8h9A1.5 1.5 0 0 1 16 9.5v9a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 4 18.5v-9A1.5 1.5 0 0 1 5.5 8Z",
  trash: "M4 7h16M10 7V5.5A1.5 1.5 0 0 1 11.5 4h1A1.5 1.5 0 0 1 14 5.5V7m3 0-.7 11.3A1.5 1.5 0 0 1 14.8 19.7H9.2a1.5 1.5 0 0 1-1.5-1.4L7 7m3.5 4v5m3-5v5",
  chevron: "m9 6 6 6-6 6",
  back: "M15 6l-6 6 6 6",
  folder: "M3.5 7.5A1.5 1.5 0 0 1 5 6h4l2 2h8a1.5 1.5 0 0 1 1.5 1.5v8A1.5 1.5 0 0 1 19 19H5a1.5 1.5 0 0 1-1.5-1.5v-10Z",
  send: "M4 12h13M13 7l5 5-5 5",
  close: "M6 6l12 12M18 6 6 18",
  sliders: "M4 7h9M17 7h3M4 17h3M11 17h9M15 5v4M9 15v4",
  plus: "M12 5v14M5 12h14",
  file: "M13.5 3.5h-6A1.5 1.5 0 0 0 6 5v14a1.5 1.5 0 0 0 1.5 1.5h9A1.5 1.5 0 0 0 18 19V8m-4.5-4.5L18 8m-4.5-4.5V8H18",
  mic: "M12 4a3 3 0 0 1 3 3v5a3 3 0 0 1-6 0V7a3 3 0 0 1 3-3Zm-6 8a6 6 0 0 0 12 0M12 18v2",
  refresh: "M23 4v6h-6M1 20v-6h6M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15",
  pencil: "M12 20h9M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5Z",
  check: "M20 6 9 17l-5-5",
};

export const Icon = ({ name, size = 16 }) => html`
  <svg class="icon" width=${size} height=${size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
    stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <path d=${ICONS[name]} />
  </svg>`;

// -- primitives -------------------------------------------------------------

export const Button = ({ variant = "secondary", size, icon, children, class: extra = "", ...props }) => html`
  <button type="button" class=${`btn btn-${variant} ${size ? `btn-${size}` : ""} ${extra}`} ...${props}>
    ${icon && html`<${Icon} name=${icon} />`}
    ${children && html`<span>${children}</span>`}
  </button>`;

export const IconButton = ({ icon, label, ...props }) => html`
  <button type="button" class="btn btn-ghost btn-icon" title=${label} aria-label=${label} ...${props}>
    <${Icon} name=${icon} />
  </button>`;

export const Pill = ({ tone, children }) => html`<span class=${`pill pill-${tone}`}>${children}</span>`;

export const Field = ({ label, hint, children }) => html`
  <label class="field">
    <span class="field-label">${label}</span>
    ${children}
    ${hint && html`<span class="field-hint">${hint}</span>`}
  </label>`;

export const Switch = ({ label, hint, checked, onChange }) => html`
  <label class="switch-row">
    <span class="switch-text">
      <span class="switch-label">${label}</span>
      ${hint && html`<span class="field-hint">${hint}</span>`}
    </span>
    <input type="checkbox" role="switch" class="switch" checked=${checked}
      onChange=${(e) => onChange(e.currentTarget.checked)} />
  </label>`;

export const Avatar = ({ label, meeting }) => {
  const name = speakerName(label, meeting);
  return html`<span class="avatar" data-hue=${speakerHue(label, meeting.speakers)} title=${name}>
    ${initials(name)}
  </span>`;
};

export const Empty = ({ title, children }) => html`
  <div class="empty">
    <div class="empty-mark"><${Icon} name="mic" size=${22} /></div>
    <div class="empty-title">${title}</div>
    <div class="empty-body">${children}</div>
  </div>`;


// -- toasts -----------------------------------------------------------------

export function useToasts() {
  const [toasts, setToasts] = useState([]);
  const push = useCallback((text, tone = "ok") => {
    const id = Math.random();
    setToasts((all) => [...all, { id, text, tone }]);
    setTimeout(() => setToasts((all) => all.filter((t) => t.id !== id)), 3200);
  }, []);
  return [toasts, push];
}

export const Toasts = ({ toasts }) => html`
  <div class="toasts" role="status">
    ${toasts.map((t) => html`<div key=${t.id} class=${`toast toast-${t.tone}`}>${t.text}</div>`)}
  </div>`;


/** A microphone with fly wings. Mirrors favicon.svg. */
export const Logo = ({ size = 28 }) => html`
  <svg class="logo" width=${size} height=${size} viewBox="0 0 32 32" aria-hidden="true">
    <rect width="32" height="32" rx="9" fill="var(--logo-tile)" />
    <ellipse cx="9" cy="10" rx="3.4" ry="7" transform="rotate(-56 9 10)" fill="var(--logo-wing)" fill-opacity=".85" />
    <ellipse cx="23" cy="10" rx="3.4" ry="7" transform="rotate(56 23 10)" fill="var(--logo-wing)" fill-opacity=".85" />
    <rect x="12.6" y="5.5" width="6.8" height="12" rx="3.4" fill="var(--logo-ink)" />
    <path d="M14.4 9.6h3.2M14.4 12.2h3.2" stroke="var(--logo-tile)" stroke-width="1.1" stroke-linecap="round" />
    <path d="M9.6 15a6.4 6.4 0 0 0 12.8 0M16 21.4v3.4M12.8 24.8h6.4" stroke="var(--logo-ink)" stroke-width="1.7" stroke-linecap="round" fill="none" />
  </svg>`;


// -- native shell -----------------------------------------------------------

/** Present when the page runs inside the app's WKWebView, not a browser. */
const bridge = window.webkit?.messageHandlers?.fly;

/** True inside the app; in a browser tab there is no shell to quit. */
export const inApp = Boolean(bridge);

/** Ask the shell for something only it can do: resize, close, open the window, quit. */
export function native(message) {
  if (bridge) bridge.postMessage(message);
  else if (message.type === "open") window.open(`/${message.route || ""}`, "_blank");
}

// -- filing form ------------------------------------------------------------

/**
 * State and actions for filing one recording. The dashboard shows it in a
 * dialog, the popover inline; both render <FilingFields>.
 */
export function useFilingForm(meeting, projects, { onFiled, onSkipped }) {
  const [title, setTitle] = useState(meeting.state.title || meeting.title || "");
  const [names, setNames] = useState({ ...(meeting.state.speaker_names || {}) });
  const [merges, setMerges] = useState({ ...(meeting.state.speaker_merges || {}) });
  // The last choice, if that project is still configured.
  const [project, setProject] = useState(
    [meeting.state.pending_project, meeting.filed[0]?.project]
      .find((name) => projects.some((p) => p.name === name)) || projects[0]?.name || "",
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const submit = async (event) => {
    event?.preventDefault();
    setBusy(true);
    setError("");
    const speaker_names = Object.fromEntries(
      Object.entries(names).map(([k, v]) => [k, v.trim()]).filter(([k, v]) => v && !merges[k]),
    );
    try {
      await api("/api/file", {
        meeting: meeting.name,
        project,
        title: title.trim(),
        speaker_names,
        speaker_merges: merges,
      });
      onFiled(project);
    } catch (err) {
      setError(err.message);
      setBusy(false);
    }
  };

  const skip = async () => {
    try {
      await api("/api/dismiss", { meeting: meeting.name });
      onSkipped();
    } catch (err) {
      setError(err.message);
    }
  };

  return {
    title, setTitle, names, setNames, merges, setMerges,
    project, setProject, busy, error, submit, skip,
  };
}

/** What the save form merges from: every label diarization produced. */
const rawSpeakers = (meeting) => meeting.raw_speakers || meeting.speakers;

/** Merge targets that keep the map acyclic: not already merged away, and not something merged into this one. */
const mergeTargets = (form, meeting, label) =>
  rawSpeakers(meeting).filter((other) =>
    other !== label && !form.merges[other] && !Object.values(form.merges).includes(label));

const setMerge = (form, label, target) => {
  const next = { ...form.merges };
  if (target) next[label] = target;
  else delete next[label];
  form.setMerges(next);
};

const nameOf = (form, label) => form.names[label] || prettyLabel(label);

/**
 * Merging is the exception, so it hides behind a text button: the picker shows
 * only when asked for, and a merged speaker reads as a sentence with an undo.
 */
const SpeakerMerge = ({ form, meeting, label }) => {
  const [open, setOpen] = useState(false);
  const target = form.merges[label];
  if (target) {
    return html`
      <div class="merge-line">
        <span>Same person as <strong>${nameOf(form, target)}</strong></span>
        <button type="button" class="text-btn" onClick=${() => setMerge(form, label, "")}>Undo</button>
      </div>`;
  }
  const targets = mergeTargets(form, meeting, label);
  if (!targets.length) return null;
  if (!open) {
    return html`<button type="button" class="text-btn" onClick=${() => setOpen(true)}>
      Merge with another speaker</button>`;
  }
  return html`
    <div class="merge-line">
      <select class="input input-sm" value=""
        aria-label=${`Which speaker is ${nameOf(form, label)} the same person as?`}
        onChange=${(e) => { setMerge(form, label, e.currentTarget.value); setOpen(false); }}>
        <option value="" disabled>Same person as…</option>
        ${targets.map((other) => html`<option key=${other} value=${other}>${nameOf(form, other)}</option>`)}
      </select>
      <button type="button" class="text-btn" onClick=${() => setOpen(false)}>Cancel</button>
    </div>`;
};

const ADD_PROJECT = "__add_project__";

/** The two ways to get a first destination: shown before anything is typed. */
export const NoProjects = ({ onAddProject }) => html`
  <div class="no-projects">
    <div>
      <div class="field-label">Where should transcripts go?</div>
      <p class="field-hint">FLY saves each transcript into a project folder, for the AI agent
        working there to turn into notes. You don't have a project yet.</p>
    </div>
    <div class="choice-row">
      <${Button} size="sm" icon="plus" onClick=${() => onAddProject("new")}>Create a new project<//>
      <${Button} size="sm" icon="folder" onClick=${() => onAddProject("existing")}>Use an existing folder<//>
    </div>
  </div>`;

export const FilingFields = ({ form, meeting, projects, onAddProject }) => html`
  <div class="stack">
    ${!projects.length && html`<${NoProjects} onAddProject=${onAddProject} />`}
    <${Field} label="Title" hint="Names the saved note.">
      <input class="input" value=${form.title} placeholder="Weekly sync"
        onInput=${(e) => form.setTitle(e.currentTarget.value)} />
    <//>

    ${rawSpeakers(meeting).length > 0 && html`
      <div class="field">
        <span class="field-label">Who is who</span>
        <div class="speaker-map">
          ${rawSpeakers(meeting).map((label) => html`
            <div class="speaker-map-row" key=${label}>
              <span class="avatar" data-hue=${speakerHue(label, rawSpeakers(meeting))}>${initials(label)}</span>
              <div class="speaker-map-fields">
                ${!form.merges[label] && html`
                  <input class="input" value=${form.names[label] || ""} placeholder=${prettyLabel(label)}
                    onInput=${(e) => form.setNames({ ...form.names, [label]: e.currentTarget.value })} />
                  ${meeting.samples?.[label] && html`<span class="quote" title=${meeting.samples[label]}>“${meeting.samples[label]}”</span>`}`}
                ${rawSpeakers(meeting).length > 1 && html`
                  <${SpeakerMerge} form=${form} meeting=${meeting} label=${label} />`}
              </div>
            </div>`)}
        </div>
        <span class="field-hint">Named speakers become the note's participants. Names and merges apply to the saved copy only. Merge a speaker that was split in two.</span>
      </div>`}

    ${projects.length > 0 && html`
      <${Field} label="Save to">
        <select class="input" value=${form.project}
          onChange=${(e) => e.currentTarget.value === ADD_PROJECT
            ? (e.currentTarget.value = form.project, onAddProject?.(null))
            : form.setProject(e.currentTarget.value)}>
          ${projects.map((p) => html`<option key=${p.name} value=${p.name}>${p.name}</option>`)}
          ${onAddProject && html`<option value=${ADD_PROJECT}>Add a project…</option>`}
        </select>
      <//>`}

    ${form.error && html`<div class="error-text">${form.error}</div>`}
  </div>`;

// Thin bar along the bottom edge of a busy status; hidden until a prediction exists.
export const RunBar = ({ progress }) =>
  progress == null ? null : html`
    <span class="run-bar" aria-hidden="true">
      <span style=${{ width: `${Math.round(progress * 100)}%` }}></span>
    </span>`;
