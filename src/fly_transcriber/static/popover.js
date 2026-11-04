// The menubar popover: record, the latest recordings, and filing inline.
// Hosted in a transparent WKWebView over the system popover material; in a
// browser (scripts/dashboard_preview.py → /popover.html) it degrades to a
// plain page.
import { html, render, useEffect, useRef, useState } from "./vendor/htm-preact-3.1.1.js";
import {
  BRAND, BRAND_TEXT, Button, FilingFields, Icon, IconButton, Logo, Pill, RunBar, Toasts, api, awaitsFiling,
  PROCESS_BUSY_HINT, fmtDuration, inApp, native, processMeeting, runBusy, status, useFilingForm, useToasts,
} from "./common.js";

const POLL_MS = 1000;
const LIMIT = 6;

const open = (route) => native({ type: "open", route });

// -- record control ---------------------------------------------------------

const RecordControl = ({ run, onToggle }) => {
  if (run.css === "busy") {
    return html`
      <div class="rec rec-busy" role="status">
        <span class="rec-dot"></span>
        <span class="rec-label">${run.label}…</span>
        <span class="rec-aside">${run.detail}</span>
        <${RunBar} progress=${run.progress} />
      </div>`;
  }
  const live = run.css === "recording";
  return html`
    <button type="button" class=${`rec ${live ? "rec-live" : ""}`} onClick=${onToggle}>
      <span class="rec-dot"></span>
      <span class="rec-label">${live ? "Stop recording" : "Start recording"}</span>
      ${live && html`<span class="rec-aside">${run.elapsed}</span>`}
    </button>`;
};

// -- list -------------------------------------------------------------------

const Row = ({ meeting, onFile, onProcess, busy }) => {
  const st = status(meeting);
  const waiting = awaitsFiling(meeting);
  const projects = [...new Set(meeting.filed.map((f) => f.project))];
  const meta = [meeting.when, meeting.duration ? fmtDuration(meeting.duration) : null]
    .filter(Boolean).join(" · ");
  const unprocessed = meeting.needs_processing;
  const action = waiting
    ? () => onFile(meeting)
    : unprocessed
      ? busy ? null : () => onProcess(meeting)
      : meeting.has_transcript && !meeting.processing
      ? () => open(`#/r/${encodeURIComponent(meeting.name)}`)
      : null;

  return html`
    <li>
      <button type="button" class="prow" disabled=${!action} onClick=${action}
        title=${waiting ? "Save this recording"
          : unprocessed ? (busy ? PROCESS_BUSY_HINT : "Transcribe this recording")
          : action ? "Open transcript" : ""}>
        <span class="prow-main">
          <span class="prow-title">${meeting.title || "Untitled recording"}</span>
          <span class="prow-meta">
            ${meta}${projects.length > 0 && html`<span class="prow-filed"> → ${projects.join(", ")}</span>`}
          </span>
        </span>
        <${Pill} tone=${st.tone}>${waiting ? "Save" : unprocessed ? "Process" : st.label}<//>
        ${action && html`<span class="prow-chevron"><${Icon} name="chevron" size=${14} /></span>`}
      </button>
    </li>`;
};

const ListView = ({ state, run, onFile, onProcess, onToggle }) => {
  const meetings = state.meetings.slice(0, LIMIT);
  const waiting = state.meetings.filter(awaitsFiling).length;
  const unprocessed = state.meetings.filter((m) => m.needs_processing).length;
  const aside = [unprocessed && `${unprocessed} to process`, waiting && `${waiting} to save`]
    .filter(Boolean).join(" · ");
  return html`
    <header class="phead">
      <span class="pbrand">
        <${Logo} size=${22} /><span>${BRAND.name}</span>
        <span class="pbrand-tagline">${BRAND_TEXT}</span>
      </span>
      <${IconButton} icon="sliders" label="Settings" onClick=${() => open("#/settings")} />
    </header>

    <${RecordControl} run=${run} onToggle=${onToggle} />
    ${run.css === "failed" && html`<div class="error-text">${run.detail || "The last recording failed."}</div>`}

    <section>
      <div class="psection">
        <h3>Recent</h3>
        ${aside && html`<span class="psection-aside">${aside}</span>`}
      </div>
      ${meetings.length
        ? html`<ul class="plist">${meetings.map((m) => html`<${Row} key=${m.name} meeting=${m}
            onFile=${onFile} onProcess=${onProcess} busy=${runBusy(run)} />`)}</ul>`
        : html`<p class="pempty">No recordings yet. Finished recordings show up here.</p>`}
    </section>

    <footer class="pfoot">
      <${Button} variant="ghost" size="sm" onClick=${() => open("#/")}>Open FLY<//>
      ${inApp && html`<${Button} variant="ghost" size="sm" onClick=${() => native({ type: "quit" })}>Quit<//>`}
    </footer>`;
};

// -- filing -----------------------------------------------------------------

const FilingView = ({ meeting, projects, onDone, toast }) => {
  const form = useFilingForm(meeting, projects, {
    onFiled: (project) => { toast(`Saved to ${project}`); onDone(); },
    onSkipped: () => { toast("Skipped"); onDone(); },
  });
  const ref = useRef();
  useEffect(() => { ref.current.querySelector("input")?.focus(); }, []);
  // Setting up a project needs the folder picker, which would close this
  // (transient) popover, so it continues in the FLY window.
  const addProject = (kind) => open(`#/save/${encodeURIComponent(meeting.name)}/${kind || "add"}`);

  return html`
    <form ref=${ref} class="pfile" onSubmit=${form.submit}>
      <header class="phead">
        <button type="button" class="pback" onClick=${onDone}>
          <${Icon} name="back" size=${14} /> Recent
        </button>
      </header>
      <div>
        <h2>Save recording</h2>
        <p class="subtle">${[meeting.when, meeting.duration && fmtDuration(meeting.duration)].filter(Boolean).join(" · ")}</p>
      </div>

      <${FilingFields} form=${form} meeting=${meeting} projects=${projects}
        onAddProject=${addProject} />

      <footer class="pfoot">
        <${Button} variant="ghost" size="sm" onClick=${form.skip}
          title="Stop counting this one as waiting to be saved">Don't save<//>
        <button type="submit" class="btn btn-primary btn-sm" disabled=${form.busy || !projects.length}>
          ${form.busy ? "Saving…" : `Save to ${form.project || "…"}`}
        </button>
      </footer>
    </form>`;
};

// -- app --------------------------------------------------------------------

const DISCONNECTED = { css: "failed", label: "Disconnected", detail: "FLY isn't responding. Is it running?" };

function App() {
  const [state, setState] = useState(null);
  const [run, setRun] = useState({ css: "idle", label: "" });
  const [filing, setFiling] = useState(null);
  const [toasts, toast] = useToasts();
  const root = useRef();

  const refresh = async () => {
    try {
      const next = await api("/api/state");
      setState(next);
      setRun(next.run);
    } catch {
      setRun(DISCONNECTED);
    }
  };

  useEffect(() => {
    let timer;
    const loop = async () => { await refresh(); timer = setTimeout(loop, POLL_MS); };
    loop();
    // The shell announces each opening, so what shows is never a second old.
    addEventListener("fly:shown", refresh);
    return () => { clearTimeout(timer); removeEventListener("fly:shown", refresh); };
  }, []);

  // The popover takes its height from the page.
  useEffect(() => {
    const observer = new ResizeObserver(([entry]) =>
      native({ type: "resize", height: Math.ceil(entry.borderBoxSize[0].blockSize) }));
    observer.observe(root.current);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const onKey = (e) => {
      if (e.key !== "Escape") return;
      if (filing) setFiling(null);
      else native({ type: "close" });
    };
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
  }, [filing]);

  const toggle = () => api("/api/record", {}).then(refresh).catch((e) => toast(e.message, "error"));
  // Look the meeting up fresh on each poll, so the form never files stale data.
  const meeting = filing && state?.meetings.find((m) => m.name === filing);

  return html`
    <div class="pop" ref=${root}>
      ${!state
        ? html`<div class="loading">${run.css === "failed" ? run.detail : "Loading…"}</div>`
        : meeting
          ? html`<${FilingView} key=${filing} meeting=${meeting} projects=${state.projects}
              toast=${toast} onDone=${() => setFiling(null)} />`
          : html`<${ListView} state=${state} run=${run} onToggle=${toggle}
              onFile=${(m) => setFiling(m.name)}
              onProcess=${(m) => processMeeting(m, toast, refresh)} />`}
    </div>
    <${Toasts} toasts=${toasts} />`;
}

render(html`<${App} />`, document.getElementById("root"));
