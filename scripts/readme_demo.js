// Stage for the README demo GIF (docs/demo.gif), rendered by
// scripts/make_demo_gif.py, which loads this into the real popover page.
//
// It swaps the HTTP API for a scripted story, draws a flat stand-in desktop
// (wallpaper, menubar, frosted panel in place of the native popover glass)
// and exposes `demo.next()`, which plays one step and returns how long that
// frame should be shown. `demo.play()` runs the whole story in real time, for
// watching it in a browser: open /popover.html from
// scripts/dashboard_preview.py at 720×620 and paste this into the console.
(() => {
  // -- scripted backend ------------------------------------------------------

  const S0 = "SPEAKER_00", S1 = "SPEAKER_01";
  const D = { phase: "idle", sec: 0, label: "Transcribing", filed: null };

  const meeting = (name, title, when, duration, filed, extra = {}) => ({
    name, title, when, duration, has_transcript: true, has_audio: false, processing: false,
    speakers: [S0, S1],
    samples: { [S0]: "Is the release still on for Friday?", [S1]: "Yes, if the tests pass." },
    filed: filed ? [{ project: filed, path: `/Users/me/${filed}/x.md`, at: "" }] : [],
    state: { title: "", speaker_names: {}, pending_project: "", dismissed: false },
    ...extra,
  });

  const clock = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;

  function snapshot() {
    const older = [
      meeting("design", "Design review", "29.09. 09:30", 1800, "Globex"),
      meeting("weekly", "Weekly sync", "28.09. 14:20", 3420, "Acme"),
    ];
    const fresh = meeting("new", D.filed?.title || "", "30.09. 10:00", 1934, D.filed?.project, {
      processing: D.phase === "busy",
      has_transcript: D.phase !== "busy" && D.phase !== "saved",
      needs_processing: D.phase === "saved",
    });
    const run = {
      idle: { css: "idle", label: "Idle", detail: "", elapsed: "" },
      rec: { css: "recording", label: "Recording", detail: "", elapsed: clock(D.sec) },
      busy: { css: "busy", label: D.label, detail: "32:14 captured", elapsed: "32:14" },
    }[["saved", "ready", "filed"].includes(D.phase) ? "idle" : D.phase];
    return {
      run,
      meetings: D.phase === "idle" || D.phase === "rec" ? older : [fresh, ...older],
      projects: [
        { name: "Acme", path: "/Users/me/acme/meetings/_inbox", naming: "vault", frontmatter: "obsidian" },
        { name: "Globex", path: "/Users/me/globex/meetings/_inbox", naming: "timestamp", frontmatter: "generic" },
      ],
      settings: {},
    };
  }

  const reply = (body) => new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
  window.fetch = async (url, opts = {}) => {
    const path = String(url).split("?")[0];
    if (path === "/api/state") return reply(snapshot());
    if (path === "/api/file") {
      const p = JSON.parse(opts.body);
      D.filed = { project: p.project, title: p.title };
      D.phase = "filed";
      setBar("idle");
      return reply({ path: "x", project: p.project });
    }
    return reply({ ok: true }); // record is driven by the story, not the button
  };

  // -- stage -----------------------------------------------------------------

  const W = 720, PANEL = 360, TOP = 38;
  const left = W - PANEL - 24, iconX = left + PANEL / 2;
  const glyph = `<svg viewBox="2 2 28 25.5" width="15" height="14"><g fill="currentColor"><ellipse cx="9.3" cy="10.5" rx="3.2" ry="6.6" transform="rotate(-56 9.3 10.5)" opacity=".5"/><ellipse cx="22.7" cy="10.5" rx="3.2" ry="6.6" transform="rotate(56 22.7 10.5)" opacity=".5"/><rect x="12.4" y="5" width="7.2" height="12.6" rx="3.6"/></g><path d="M9.4 15a6.6 6.6 0 0 0 13.2 0M16 21.6V25M12.6 25h6.8" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" fill="none"/></svg>`;
  const wave = `<svg viewBox="0 0 16 18" width="14" height="14"><path d="M2 8v2M5 5v8M8 2v14M11 5v8M14 7v4" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" fill="none"/></svg>`;

  const style = document.createElement("style");
  style.textContent = `
    html { background: #dcd8c3 !important; overflow: hidden; }
    .demo-shape { position: fixed; z-index: 0; }
    .demo-bar { position: fixed; z-index: 3; left: 0; right: 0; top: 0; height: 26px; display: flex; align-items: center;
      justify-content: flex-end; gap: 18px; padding: 0 14px; font: 500 13px -apple-system, system-ui; color: #1b2228;
      background: rgba(255,255,255,.5); -webkit-backdrop-filter: blur(20px); backdrop-filter: blur(20px); }
    .demo-icon { position: fixed; z-index: 4; top: 2px; height: 22px; display: flex; align-items: center; gap: 4px;
      padding: 0 7px; border-radius: 6px; font: 500 13px -apple-system, system-ui; font-variant-numeric: tabular-nums;
      color: #1b2228; background: rgba(0,0,0,.08); transform: translateX(-50%); }
    .demo-icon.is-rec { color: #d0342c; }
    #root { position: fixed !important; z-index: 2; top: ${TOP}px; left: ${left}px; width: ${PANEL}px;
      border-radius: 16px; border: 1px solid rgba(0,0,0,.09); background: rgba(246,248,249,.66);
      -webkit-backdrop-filter: blur(30px) saturate(170%); backdrop-filter: blur(30px) saturate(170%); }
    .demo-cursor { position: fixed; z-index: 9; width: 20px; height: 22px; pointer-events: none; display: none; }
    * { transition: none !important; }
  `;
  document.head.appendChild(style);

  // Flat wallpaper: a few palette shapes, so the frosted panel reads as glass.
  for (const [css, color] of [
    ["left:-60px; top:180px; width:420px; height:420px; border-radius:50%", "#3d405b"],
    ["left:250px; top:-90px; width:300px; height:300px; border-radius:50%", "#81b29a"],
    ["left:470px; top:300px; width:360px; height:360px; border-radius:50%", "#e07a5f"],
    ["left:590px; top:40px; width:160px; height:160px; border-radius:40px", "#f2cc8f"],
  ]) {
    const d = document.createElement("div");
    d.className = "demo-shape";
    d.style.cssText = css + `; background:${color}`;
    document.body.appendChild(d);
  }

  const bar = document.createElement("div");
  bar.className = "demo-bar";
  bar.innerHTML = `<span style="width:20px"></span><span>Wed 30 Sep 10:32</span>`;
  document.body.appendChild(bar);
  const icon = document.createElement("div");
  icon.className = "demo-icon";
  icon.style.left = iconX + "px";
  document.body.appendChild(icon);
  function setBar(kind, text = "") {
    icon.className = "demo-icon" + (kind === "rec" ? " is-rec" : "");
    icon.innerHTML = (kind === "busy" ? wave : glyph) + (text ? `<span>${text}</span>` : "");
  }
  setBar("idle");

  const cursor = document.createElement("div");
  cursor.className = "demo-cursor";
  cursor.innerHTML = `<svg viewBox="0 0 20 22"><path d="M3 1.5l13 9-5.8 1.1 3.4 6.4-2.5 1.3-3.4-6.5L3 17z" fill="#111" stroke="#fff" stroke-width="1.4" stroke-linejoin="round"/></svg>`;
  document.body.appendChild(cursor);
  function pointAt(el, dx = 0.7) {
    const r = el.getBoundingClientRect();
    cursor.style.display = "block";
    cursor.style.left = r.left + r.width * dx + "px";
    cursor.style.top = r.top + r.height * 0.55 - 3 + "px";
  }
  const hideCursor = () => { cursor.style.display = "none"; };

  // -- story -----------------------------------------------------------------

  const $ = (sel) => document.querySelector(sel);
  // Re-poll now, then wait for the result to be painted before a capture.
  const refresh = () => new Promise((done) => {
    dispatchEvent(new Event("fly:shown"));
    setTimeout(() => requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(done, 250))), 160);
  });
  const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
  const type = (el, text) => { el.focus(); setValue.call(el, text); el.dispatchEvent(new Event("input", { bubbles: true })); };
  const inputs = () => [...document.querySelectorAll(".pfile input")];
  const typing = (index, text, cuts) => cuts.map((n, i) => [i === cuts.length - 1 ? 420 : 150, () => type(inputs()[index], text.slice(0, n))]);

  const steps = [
    [1300, () => {}],
    [800, () => pointAt($(".rec"))],
    [650, () => { D.phase = "rec"; D.sec = 0; setBar("rec", "0:00"); }],
    ...[1, 2, 3].map((s) => [650, () => { D.sec = s; setBar("rec", clock(s)); }]),
    // Stopping saves the recording; it is processed when the user asks.
    [900, () => { hideCursor(); D.phase = "saved"; setBar("idle", "1"); }],
    [700, () => pointAt($(".prow"), 0.5)],
    [700, () => { hideCursor(); $(".prow").click(); D.phase = "busy"; D.label = "Transcribing"; setBar("busy"); }],
    [1000, () => { D.label = "Diarizing"; }],
    [1500, () => { D.phase = "ready"; setBar("idle", "1"); }],
    [700, () => pointAt($(".prow"), 0.5)],
    [800, () => { hideCursor(); $(".prow").click(); }],
    ...typing(0, "Release sync", [3, 7, 10, 12]),
    ...typing(1, "Aino", [2, 4]),
    ...typing(2, "Mikko", [3, 5]),
    [800, () => { document.activeElement.blur(); pointAt($(".pfile .btn-primary"), 0.6); }],
    [2600, () => { hideCursor(); $(".pfile .btn-primary").click(); }],
  ];

  let i = 0;
  window.demo = {
    count: steps.length,
    async next() {
      const [ms, run] = steps[i++];
      await run();
      await refresh();
      return ms;
    },
    async play() {
      i = 0;
      while (i < steps.length) {
        const ms = await this.next();
        await new Promise((r) => setTimeout(r, ms));
      }
    },
  };
})();
