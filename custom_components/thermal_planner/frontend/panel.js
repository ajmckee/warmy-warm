/* Local HA custom panel. No external scripts, fonts, build step, or credentials. */
const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[c]);
const number = (value, digits = 2) => typeof value === "number" && Number.isFinite(value)
  ? value.toFixed(digits) : "—";
const percent = (value) => typeof value === "number" ? `${Math.round(value * 100)}%` : "—";
const colors = ["#147d92", "#af5c13", "#8561b5", "#b04b76", "#448145", "#396fc2", "#b26053", "#77732b"];

class ThermalPlannerPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.entries = [];
    this.report = null;
    this.busy = false;
    this.pages = ["reports", "measurements", "learning", "heating-plan"];
    this.onPageChange = () => this.selectPage(window.location.hash.slice(1));
  }

  set hass(value) {
    this._hass = value;
    this.updateMenu();
    if (this.isConnected && this.mounted && !this.initialized && !this.loading) this.refresh();
  }
  set narrow(value) { this._narrow = value; this.updateMenu(); }
  updateMenu() {
    if (this.mounted) this.$("#menu").hidden = !(this._narrow || this._hass?.dockedSidebar === "always_hidden");
  }

  get zone() { return this._hass?.config?.time_zone || "UTC"; }
  get entry() { return this.entries.find((entry) => entry.entry_id === this.entryId); }
  $(selector) { return this.shadowRoot.querySelector(selector); }
  time(value) {
    return new Intl.DateTimeFormat(undefined, {
      timeZone: this.zone, dateStyle: "medium", timeStyle: "short",
    }).format(new Date(value));
  }
  inputTime(date) {
    return new Intl.DateTimeFormat("sv-SE", {
      timeZone: this.zone, year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    }).format(date).replace(" ", "T");
  }
  async call(type, data = {}) { return this._hass.callWS({ type: `thermal_planner/${type}`, ...data }); }
  message(text = "", error = false) {
    const element = this.$("#message");
    element.textContent = text;
    element.className = error ? "notice error" : "notice";
    element.setAttribute("role", error ? "alert" : "status");
    element.hidden = !text;
  }

  connectedCallback() {
    if (!this.mounted) this.mount();
    window.addEventListener("hashchange", this.onPageChange);
    this.onPageChange();
    if (this._hass) this.refresh();
    this.timer = setInterval(() => {
      if (!this.busy && !this.loading && !this.setupOpen && this._hass) this.refresh(true);
    }, 10000);
  }
  disconnectedCallback() { clearInterval(this.timer); window.removeEventListener("hashchange", this.onPageChange); }

  selectPage(page) {
    this.page = this.pages.includes(page) ? page : "reports";
    this.pages.forEach(name => {
      const selected = name === this.page;
      this.$(`#page-${name}`).hidden = !selected;
      const tab = this.$(`#tab-${name}`);
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
    });
  }

  mount() {
    this.mounted = true;
    this.shadowRoot.innerHTML = `
      <style>
        :host { display:block; min-height:100%; color:var(--primary-text-color,#203538);
          background:var(--primary-background-color,#f3f6f5); font-family:var(--paper-font-body1_-_font-family,system-ui,sans-serif); }
        * { box-sizing:border-box; } main { max-width:1380px; margin:auto; padding:32px; }
        header { display:flex; gap:16px; align-items:center; justify-content:space-between; margin-bottom:24px; }
        .heading { display:flex; gap:12px; align-items:center; min-width:0; } #menu { padding:8px 12px; font-size:20px; }
        h1 { font-size:28px; margin:0 0 6px; font-weight:650; letter-spacing:-.6px; }
        h2 { font-size:19px; margin:0 0 14px; } h3 { font-size:16px; margin:0 0 10px; }
        p { line-height:1.6; margin:8px 0; } .muted, small { color:var(--secondary-text-color,#607477); }
        .eyebrow { font-size:11px; text-transform:uppercase; letter-spacing:1.6px; color:var(--primary-color,#147d92); font-weight:700; margin-bottom:8px; }
        a { color:var(--primary-color,#147d92); } button, input, select { font:inherit; border-radius:8px; }
        button { cursor:pointer; border:1px solid var(--divider-color,#ccd8d6); padding:10px 15px;
          background:var(--card-background-color,#fff); color:var(--primary-text-color,#203538); }
        button.primary { background:var(--primary-color,#147d92); border-color:transparent; color:var(--text-primary-color,#fff); }
        button:disabled { opacity:.5; cursor:default; } button:focus-visible, a:focus-visible, input:focus-visible, select:focus-visible { outline:3px solid var(--primary-color,#147d92); outline-offset:3px; }
        input, select { padding:10px; border:1px solid var(--divider-color,#ccd8d6); width:100%; min-width:0;
          background:var(--card-background-color,#fff); color:var(--primary-text-color,#203538); }
        label { font-size:13px; display:block; margin-bottom:6px; } .card { background:var(--card-background-color,#fff);
          border:1px solid var(--divider-color,#dce5e2); border-radius:14px; padding:22px; margin-bottom:20px; }
        .summary { display:grid; grid-template-columns:repeat(4,1fr); gap:14px; }
        .summary .card { padding:18px; } .value { font-size:24px; font-weight:650; margin-top:9px; overflow-wrap:anywhere; }
        .layout { display:grid; grid-template-columns:290px minmax(0,1fr); gap:22px; align-items:start; }
        .layout > * { min-width:0; } .row > div { min-width:0; max-width:100%; }
        form.analyse { display:grid; grid-template-columns:1fr 1fr auto; gap:14px; align-items:end; }
        .row { display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:12px; }
        .notice { padding:14px 18px; background:var(--secondary-background-color,#eaf3f1); border-radius:8px; margin-bottom:18px; }
        .error { border-left:4px solid var(--error-color,#c1453d); } .empty { padding:30px 12px; text-align:center; }
        .saved { width:100%; text-align:left; margin-top:10px; padding:13px; line-height:1.5; }
        .saved.active { border:2px solid var(--primary-color,#147d92); background:var(--secondary-background-color,#eef5f4); padding:12px; }
        .saved small { display:block; margin-top:4px; } .badge { display:inline-block; padding:4px 8px; border-radius:20px;
          font-size:12px; background:var(--secondary-background-color,#edf3f0); color:var(--secondary-text-color,#496663); }
        .warning { color:var(--error-color,#ad4d29); } .scroll { overflow:auto; }
        table { border-collapse:collapse; width:100%; font-size:13px; white-space:nowrap; }
        th { text-align:left; color:var(--secondary-text-color,#607477); font-weight:500; }
        td, th { padding:12px 10px; border-bottom:1px solid var(--divider-color,#e3e9e7); }
        td:first-child, th:first-child { padding-left:0; } td:not(:first-child), th:not(:first-child) { text-align:right; }
        .chart { width:100%; display:block; margin:14px 0; overflow:visible; } .chart text { fill:var(--secondary-text-color,#607477); font-size:11px; }
        .legend { display:flex; flex-wrap:wrap; gap:10px 18px; font-size:12px; margin-bottom:12px; }
        .dot { width:9px; height:9px; display:inline-block; border-radius:50%; margin-right:6px; }
        .compact { width:auto; max-width:100%; } details summary { cursor:pointer; font-size:13px; }
        .events { padding-left:20px; font-size:13px; line-height:1.6; } .lab form { display:flex; gap:10px; align-items:end; flex-wrap:wrap; }
        .lab input { width:100px; } .lab input[type="checkbox"] { width:auto; } .lab { border-top:3px solid var(--primary-color,#147d92); }
        .tabs { display:flex; gap:4px; overflow-x:auto; margin-bottom:24px; border-bottom:1px solid var(--divider-color,#ccd8d6); }
        .tabs button { flex:0 0 auto; border:0; border-bottom:3px solid transparent; border-radius:0; background:transparent; padding:14px 16px; }
        .tabs button[aria-selected="true"] { color:var(--primary-color,#147d92); border-bottom-color:var(--primary-color,#147d92); font-weight:650; }
        .page-intro { margin-bottom:22px; } .page-intro h2 { margin-bottom:6px; }
        input[type="checkbox"] { width:auto; margin-right:8px; vertical-align:middle; }
        #measurement-setup { max-width:920px; }
        #measurement-setup select, #measurement-setup input:not([type="checkbox"]) { margin-bottom:14px; }
        [hidden] { display:none !important; }
        @media(max-width:900px) { .layout { grid-template-columns:minmax(0,1fr); } #saved-list { display:flex; gap:10px; overflow:auto; } .saved { min-width:220px; } }
        @media(max-width:600px) { main { padding:16px; } header { align-items:flex-start; } h1 { font-size:24px; }
          .tabs { display:grid; grid-template-columns:1fr 1fr; overflow:visible; gap:0; }
          .tabs button { padding:12px 8px; }
          .summary { grid-template-columns:1fr 1fr; gap:8px; } .summary .card { padding:14px; } .value { font-size:20px; }
          form.analyse { grid-template-columns:1fr; } .card { padding:16px; } header a { font-size:13px; } }
      </style>
      <main>
        <header><div class="heading"><button id="menu" type="button" aria-label="Open Home Assistant menu" hidden>☰</button><div><div class="eyebrow">House temperature analysis</div><h1>Thermal Planner</h1>
          <div class="muted">Understand how your rooms cool, together.</div></div></div>
          <a href="/config/integrations/integration/thermal_planner">Configure rooms</a></header>
        <div id="message" role="status" hidden></div>
        <div id="entry-picker" hidden><label for="entry">Configuration</label><select id="entry"></select></div>
        <div class="tabs" role="tablist" aria-label="Thermal Planner pages">
          <button id="tab-reports" role="tab" aria-controls="page-reports" aria-selected="true" data-page="reports">Reports</button>
          <button id="tab-measurements" role="tab" aria-controls="page-measurements" aria-selected="false" tabindex="-1" data-page="measurements">Measurements</button>
          <button id="tab-learning" role="tab" aria-controls="page-learning" aria-selected="false" tabindex="-1" data-page="learning">Learning</button>
          <button id="tab-heating-plan" role="tab" aria-controls="page-heating-plan" aria-selected="false" tabindex="-1" data-page="heating-plan">Heating plan</button>
        </div>
        <section id="page-reports" role="tabpanel" aria-labelledby="tab-reports" tabindex="0">
        <div class="page-intro"><h2>Reports</h2><p class="muted">Review recorded temperatures and compare how your rooms cool.</p></div>
        <section class="card" id="analyse-card"><div class="row"><h2>Create a report</h2><span class="badge">Temperature observation</span></div>
          <form class="analyse" id="analyse-form"><div><label for="start">Period start</label><input id="start" type="datetime-local" required></div>
            <div><label for="end">Period end</label><input id="end" type="datetime-local" required></div>
            <button class="primary" id="analyse" type="submit">Analyse temperatures</button></form>
          <p class="muted"><small id="time-help"></small></p><details><summary>History and outdoor temperature</summary><label for="history-policy">Use recorded readings</label><select id="history-policy"><option value="until_changed">Keep the last known reading until it changes or becomes unavailable</option><option value="max_age">Stop using readings older than my configured freshness limit</option></select><p><small>Recorder often saves changes, not repeated identical readings. Keep-last-known avoids false gaps, but cannot prove the sensor stayed online. Recorded unknown/unavailable states always create gaps. Live safety checks stay age-limited.</small></p><label for="outdoor-mode">Outdoor temperature for this report</label><select id="outdoor-mode"><option value="recorded">Use recorded outdoor / weather readings</option><option value="assumed">Use a constant temperature I supply</option></select><div id="assumed-outdoor-field" hidden><label for="assumed-outdoor">Assumed outdoor temperature (°C)</label><input id="assumed-outdoor" type="number" min="-80" max="60" step="0.1"><p><small>This assumption is saved and labelled on the report and CSV. It does not change live measurements or train a model.</small></p></div></details></section>
        <section id="overview" class="summary" aria-label="Selected report overview"></section>
        <div class="layout"><aside class="card"><h2>Saved reports</h2><small>Latest 10 reports · saved automatically</small><div id="saved-list"></div></aside>
          <section id="report" aria-label="Report details"></section></div>
        </section>
        <section id="page-measurements" role="tabpanel" aria-labelledby="tab-measurements" tabindex="0" hidden>
          <div class="page-intro"><h2>Measurements</h2><p class="muted">Record normal heating or run a controlled cooldown, then follow recovery.</p></div>
          <section id="measurement-choice" class="card"></section><section id="measurement-setup" class="card" hidden></section><div id="measurement-context"><section id="measurement-checks" class="card" hidden></section><section id="source" class="card"></section>
          <section id="lab" class="card lab" hidden></section>
          <section id="runs" class="card"></section><section id="run-detail" class="card" hidden></section></div>
        </section>
        <section id="page-learning" role="tabpanel" aria-labelledby="tab-learning" tabindex="0" hidden>
          <div class="page-intro"><h2>Learning</h2><p class="muted">See what your measurements tell us about each room and how reliable the predictions are.</p></div>
          <section id="models" class="card"></section>
        </section>
        <section id="page-heating-plan" role="tabpanel" aria-labelledby="tab-heating-plan" tabindex="0" hidden>
          <div class="page-intro"><h2>Heating plan</h2><p class="muted">Compare normal heating with a recommendation based on your rooms and the weather forecast.</p></div>
          <section id="planner" class="card"></section>
        </section>
      </main>`;
    this.shadowRoot.querySelectorAll("[data-page]").forEach(button => {
      button.addEventListener("click", () => {
        this.selectPage(button.dataset.page);
        if (window.location.hash !== `#${this.page}`) window.location.hash = this.page;
      });
      button.addEventListener("keydown", event => {
        const index = this.pages.indexOf(button.dataset.page);
        const next = { ArrowRight: (index + 1) % this.pages.length, ArrowLeft: (index + this.pages.length - 1) % this.pages.length, Home: 0, End: this.pages.length - 1 }[event.key];
        if (next === undefined) return;
        event.preventDefault();
        const tab = this.$(`#tab-${this.pages[next]}`);
        tab.click(); tab.focus();
      });
    });
    this.$("#outdoor-mode").onchange=()=>{this.$("#assumed-outdoor-field").hidden=this.$("#outdoor-mode").value!=="assumed";this.$("#assumed-outdoor").required=this.$("#outdoor-mode").value==="assumed";};
    this.$("#analyse-form").addEventListener("submit", (event) => { event.preventDefault(); this.analyse(); });
    this.$("#menu").addEventListener("click", () => this.dispatchEvent(new CustomEvent("hass-toggle-menu", { bubbles: true, composed: true })));
    this.updateMenu();
    this.$("#entry").addEventListener("change", () => {
      this.entryId = this.$("#entry").value; this.report = null; this.selected = null; this.refresh();
    });
  }

  async refresh(silent = false) {
    if (this.loading || !this._hass) return;
    this.loading = true;
    try {
      const { entries } = await this.call("reports");
      this.entries = entries;
      if (!this.entry) this.entryId = entries[0]?.entry_id;
      if (!this.initialized) {
        this.$("#end").value = this.inputTime(new Date(Date.now() - 5 * 60000));
        this.$("#start").value = this.inputTime(new Date(Date.now() - 245 * 60000));
        this.initialized = true;
      }
      this.$("#time-help").textContent = `Times use ${this.zone}. Choose 1 minute to 24 hours in the past. Saved room settings are used.`;
      this.$("#entry-picker").hidden = entries.length <= 1;
      this.$("#entry").innerHTML = entries.map((entry) => `<option value="${escapeHTML(entry.entry_id)}">${escapeHTML(entry.title)}</option>`).join("");
      this.$("#entry").value = this.entryId || "";
      if(this.lastEntryId !== this.entryId){this.lastEntryId=this.entryId;this.$("#history-policy").value=this.entry?.history_policy||"until_changed";this.setupOpen=false;this.$("#measurement-setup").hidden=true;this.$("#measurement-context").hidden=false;}
      this.$("#analyse-card").hidden = !this.entry;
      if (!this.entry) {
        this.$("#saved-list").innerHTML = "<p>No accessible configuration.</p>";
        this.$("#report").innerHTML = '<div class="card empty"><h2>Start with your rooms</h2><p>Add Thermal Planner in Settings → Devices &amp; services, then configure at least one room.</p></div>';
        this.$("#overview").innerHTML = ""; this.$("#lab").hidden = true;
        return;
      }
      const requested = new URLSearchParams(window.location.search).get("report");
      if (!this.entry.reports.some((report) => report.id === this.selected)) {
        this.selected = this.entry.reports.find((report) => report.id === requested)?.id || this.entry.reports[0]?.id;
      }
      if (this.selected && this.report?.id !== this.selected) {
        this.report = await this.call("report", { entry_id: this.entryId, report_id: this.selected });
        this.renderReport();
      } else if (!this.selected) { this.report = null; this.renderReport(); }
      this.renderList();
      this.operations = await this.call("operations", { entry_id: this.entryId });
      this.labState = this.operations.control;
      this.renderSource(); this.renderMeasurementEntry(); this.renderLab(); this.renderOperations();
    } catch (error) { if (!silent) this.message(error.message || String(error), true); }
    finally { this.loading = false; }
  }

  renderList() {
    this.$("#saved-list").innerHTML = this.entry.reports.map((report) => `
      <button class="saved ${report.id === this.selected ? "active" : ""}" aria-pressed="${report.id === this.selected}" data-report="${escapeHTML(report.id)}">
        ${escapeHTML(this.time(report.start))}<small>to ${escapeHTML(this.time(report.end))}</small>
        <small>${report.room_count} rooms · ${report.complete ? "Complete indoor data" : "Missing indoor data"}${report.breaches.length ? " · Minimum breached" : ""}</small></button>`).join("") ||
      '<p class="muted">No reports yet. Choose a period above to create your first report.</p>';
    this.shadowRoot.querySelectorAll("[data-report]").forEach((button) => button.addEventListener("click", async () => {
      try {
        const report = await this.call("report", { entry_id: this.entryId, report_id: button.dataset.report });
        this.report = report; this.selected = report.id; this.windowIndex = 0;
        this.renderReport(); this.renderList(); this.message();
      } catch (error) { this.message(error.message || String(error), true); }
    }));
  }

  async analyse() {
    if (this.busy || !this.entry) return;
    this.busy = true; this.$("#analyse").disabled = true; this.$("#analyse").textContent = "Analysing…";
    this.message("Reading recorded temperatures and calculating the report…");
    try {
      this.report = await this.call("analyse", { entry_id: this.entryId, start: this.$("#start").value, end: this.$("#end").value, history_policy:this.$("#history-policy").value, ...(this.$("#outdoor-mode").value === "assumed" ? {assumed_outdoor_c:Number(this.$("#assumed-outdoor").value)} : {}) });
      this.selected = this.report.id; this.windowIndex = 0; this.renderReport(); await this.refresh();
      this.message("Report saved. Review data coverage before interpreting cooling rates.");
    } catch (error) { this.message(error.message || String(error), true); }
    finally { this.busy = false; this.$("#analyse").disabled = false; this.$("#analyse").textContent = "Analyse temperatures"; }
  }

  chart(report, predicted = false) {
    const ts = report.traces.timestamps.map((value) => Date.parse(value));
    const series = Object.entries(report.traces.rooms);
    const values = series.flatMap(([, samples]) => samples.filter((v) => typeof v === "number" && Number.isFinite(v)));
    if (!values.length || ts.length < 2) return '<p class="muted">No covered temperature samples to plot.</p>';
    const min = Math.min(...values) - .3, max = Math.max(...values) + .3;
    const x = (i) => 46 + (ts[i] - ts[0]) / (ts.at(-1) - ts[0]) * 700;
    const y = (v) => 20 + (max - v) / (max - min) * 210;
    let svg = `<svg class="chart" viewBox="0 0 770 265" role="img" aria-labelledby="chart-title"><title id="chart-title">${predicted ? "Predicted" : "Recorded"} room temperatures in degrees Celsius. Gaps show missing or stale readings.</title>`;
    for (let i = 0; i <= 4; i++) {
      const value = min + (max - min) * i / 4;
      svg += `<line x1="46" x2="746" y1="${y(value)}" y2="${y(value)}" stroke="currentColor" opacity=".1"/><text x="38" y="${y(value) + 4}" text-anchor="end">${number(value, 1)}°</text>`;
    }
    series.forEach(([name, samples], index) => {
      let path = "", gap = true;
      samples.forEach((value, i) => {
        if(i>0 && ts[i]-ts[i-1]>600000)gap=true;
        if (typeof value !== "number" || !Number.isFinite(value)) { gap = true; return; }
        path += `${gap ? "M" : "L"}${x(i).toFixed(2)},${y(value).toFixed(2)} `; gap = false;
      });
      svg += `<path d="${path}" fill="none" stroke="${colors[index % colors.length]}" stroke-width="2.5"><title>${escapeHTML(name)}</title></path>`;
    });
    svg += `<text x="46" y="255">${escapeHTML(this.time(report.start))}</text><text x="746" y="255" text-anchor="end">${escapeHTML(this.time(report.end))}</text></svg>`;
    return svg + '<div class="legend">' + series.map(([name], i) => `<span><span class="dot" style="background:${colors[i % colors.length]}"></span>${escapeHTML(name)}</span>`).join("") + "</div>";
  }

  renderReport() {
    const r = this.report;
    if (!r) {
      this.$("#overview").innerHTML = "";
      this.$("#report").innerHTML = '<div class="card empty"><h2>Your first temperature report</h2><p>Analyse a recorded period to compare rooms and floors here. No download is needed.</p></div>';
      return;
    }
    const breaches = Object.keys(r.rooms).filter((name) => r.rooms[name].comfort_breached);
    const complete = r.windows.every((window) => Object.values(window.rooms).every((row) => row.coverage === 1));
    this.$("#overview").innerHTML = [
      ["Rooms analysed", String(Object.keys(r.rooms).length)],
      ["Indoor data", complete ? "Complete" : "Has gaps"],
      ["Below minimum", breaches.length ? `${breaches.length} room${breaches.length > 1 ? "s" : ""}` : "None observed"],
      ["First 0.5°C drop", r.limiting_room || "Not observed"],
    ].map(([label, value]) => `<div class="card"><small>${escapeHTML(label)}</small><div class="value">${escapeHTML(value)}</div></div>`).join("");
    this.windowIndex = Math.min(this.windowIndex || 0, r.windows.length - 1);
    this.$("#report").innerHTML = `
      <div class="card"><div class="row"><h2>Temperature timeline</h2><button id="export">Export CSV</button></div>
        <p class="muted">${escapeHTML(this.time(r.start))} → ${escapeHTML(this.time(r.end))} · ${escapeHTML(this.zone)}</p>
        ${r.unavailability_events?.length ? `<p class="notice">Some configured sensors recorded unknown/unavailable states (${r.unavailability_events.length} events). Their recorded values are not carried through outages.</p>` : ""}
        ${r.outdoor_assumption_c != null ? `<p class="notice"><strong>Assumed outdoor temperature: ${number(r.outdoor_assumption_c,1)}°C throughout this report.</strong> Outside-normalized cooling results use this assumption, not measured weather.</p>` : ""}
        <p><small>${r.data_policy?.history_policy === "until_changed" ? "Last known sensor readings are carried forward until the next recorded change. Recorded unavailability remains a gap; unrecorded outages cannot be detected." : "Readings expire at the configured historical freshness limit."}</small></p>
        ${this.chart(r)}
        <p><small>${escapeHTML(r.label)}. Residual heat and circulation can remain. Missing readings are shown as gaps.</small></p>
        ${r.contamination ? `<p class="warning">${escapeHTML(r.contamination)}</p>` : ""}
        ${!complete ? '<p class="warning">Some indoor readings are missing or stale. Cooling rates are withheld for incomplete comparison windows.</p>' : ""}
        ${breaches.length ? `<p class="warning">Minimum temperature breached: ${escapeHTML(breaches.join(", "))}.</p>` : ""}
      </div>
      <div class="card"><div class="row"><h2>Rooms and floors</h2><div><label for="window">Comparison window</label><select class="compact" id="window">
        ${r.windows.map((window, i) => `<option value="${i}">${escapeHTML(this.time(window.start))} → ${escapeHTML(this.time(window.end))}</option>`).join("")}</select></div></div>
        <div id="metrics" class="scroll"></div><p><small>Negative rates mean cooling. Floor averages include only assigned rooms with positive floor weights.</small></p></div>
      <div class="card"><h2>Room minimums and cooling thresholds</h2><div class="scroll"><table><thead><tr><th>Room</th><th>0.5°C drop</th><th>1°C drop</th><th>Minimum breached</th><th>Temperature coverage</th></tr></thead><tbody>
        ${Object.entries(r.rooms).map(([name, room]) => `<tr><td>${escapeHTML(name)}</td><td>${number(room.thresholds["0.5"])} h</td><td>${number(room.thresholds["1.0"])} h</td><td>${room.comfort_breached ? "Yes" : "Not observed"}</td><td>${percent(room.comfort_coverage)}</td></tr>`).join("")}
        </tbody></table></div><p><small>A missing threshold means it was not observed with continuous covered samples. Missing data cannot establish that a room stayed above its minimum.</small></p>
        <details><summary>Report settings and provenance</summary><p><small>Report ${escapeHTML(r.id)}. Outdoor source: ${escapeHTML(r.outdoor_source || "unavailable")}. This report keeps the room configuration used when it was created.</small></p>
        <ul>${r.configuration.rooms.map((room) => `<li>${escapeHTML(room.name)}: ${escapeHTML(room.sensors.join(", "))}; minimum ${number(room.comfort_min, 1)}°C; ${escapeHTML(room.floor || "no floor")}</li>`).join("")}</ul></details></div>`;
    this.$("#window").value = String(this.windowIndex);
    this.$("#window").addEventListener("change", () => { this.windowIndex = Number(this.$("#window").value); this.renderMetrics(); });
    this.$("#export").addEventListener("click", () => this.exportCSV());
    this.renderMetrics();
    this.renderExtras(r);
  }

  renderMetrics() {
    const window = this.report.windows[this.windowIndex];
    let rows = "";
    for (const [kind, label] of [["rooms", "Room"], ["floors", "Floor"]]) {
      for (const [name, row] of Object.entries(window[kind])) {
        rows += `<tr><td>${escapeHTML(name)} <small>· ${label}</small></td><td>${number(row.start_c)} → ${number(row.end_c)}</td><td>${number(row.delta_c)}</td><td>${number(row.slope_c_per_h)}</td><td>${percent(row.coverage)}</td><td>${percent(row.outside_coverage)}</td><td>${number(row.normalized_c_per_h_10k)}</td></tr>`;
      }
    }
    this.$("#metrics").innerHTML = `<table><thead><tr><th>Name</th><th>Start → end °C</th><th>Change °C</th><th>°C/h</th><th>Indoor data</th><th>Outdoor data</th><th>°C/h at 10 K gap</th></tr></thead><tbody>${rows}</tbody></table>`;
  }

  async exportCSV() {
    try {
      const csv = await this.call("export", { entry_id: this.entryId, report_id: this.report.id });
      const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
      const link = document.createElement("a"); link.href = url; link.download = `thermal-planner-${this.report.id}.csv`;
      document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { this.message(error.message || String(error), true); }
  }

  renderMeasurementEntry() {
    const root = this.$("#measurement-choice");
    if (!root) return;
    if (!this._hass.user?.is_admin){root.innerHTML="<h2>Measurement history</h2><p>You can review completed measurements below. A Home Assistant administrator can connect readings and start new measurements.</p>";return;}
    if (!root.dataset.mounted) {
      root.dataset.mounted = "true";
      root.innerHTML = `<h2>What would you like to measure?</h2><div class="row"><button id="goal-observe" class="primary">Watch normal heating</button><button id="goal-cooldown">Controlled cooldown</button></div>
        <p id="goal-help"></p><form id="observe-form"><label for="observe-minutes">Watch for (minutes)</label><input class="compact" id="observe-minutes" type="number" min="30" max="720" value="120" required><button type="submit">Observe normal operation</button></form>
        <button id="setup-measurement" style="margin-top:14px">Set up measurement</button><div id="observation-progress" role="status"></div>`;
      this.$("#goal-observe").onclick = ()=>this.setMeasurementGoal("observe");
      this.$("#goal-cooldown").onclick = ()=>this.setMeasurementGoal("cooldown");
      this.$("#setup-measurement").onclick = ()=>this.openMeasurementSetup();
      this.$("#observe-form").onsubmit = event=>{event.preventDefault();this.operation("observe",{minutes:Number(this.$("#observe-minutes").value)});};
    }
    this.measurementGoal ||= (this.operations?.control?.journal || this.operations?.observation || this.operations?.campaign?.active ? "cooldown" : "observe");
    this.$("#goal-help").textContent = this.measurementGoal === "observe" ? "Recommended first step. Record what your heating normally does. This does not change your heat pump and needs no pause/restore check." : "Pause the shared heat pump, then restore it and watch the rooms recover. Complete the checks below first. Water-temperature trials are optional and can wait.";
    this.$("#observe-form").hidden = this.measurementGoal !== "observe";
    this.$("#goal-observe").classList.toggle("primary",this.measurementGoal === "observe");
    this.$("#goal-cooldown").classList.toggle("primary",this.measurementGoal === "cooldown");
    this.$("#goal-observe").setAttribute("aria-pressed", String(this.measurementGoal === "observe"));
    this.$("#goal-cooldown").setAttribute("aria-pressed", String(this.measurementGoal === "cooldown"));
    const ops=this.operations, occupied=ops?.control?.journal || ops?.observation || ops?.baseline || ops?.campaign?.active || ops?.mode !== "advisory";
    this.$("#observe-form button").disabled = Boolean(this.busy || occupied || ops?.readiness?.can_observe === false);
    this.$("#observation-progress").innerHTML = ops?.control?.journal || ops?.observation ? `<p>A controlled measurement ${ops.observation ? "is observing recovery" : "is running"}. Use Stop to end it and restore any settings still owned by the planner.</p><button id="stop-active-measurement">Stop and restore</button>` : ops?.baseline ? `<p>Watching normal heating until ${escapeHTML(this.time(ops.baseline.deadline))}.</p><button id="stop-observation">Stop observation</button>` : ops?.campaign?.active || ops?.mode !== "advisory" ? '<p>A heating schedule is active. Stop it before starting a manual measurement.</p><button id="stop-active-measurement">Stop scheduled heating</button>' : "";
    this.$("#stop-active-measurement")?.addEventListener("click",()=>this.operation("stop"));
    this.$("#stop-observation")?.addEventListener("click",()=>this.operation("stop"));
    const ready=ops?.readiness;
    this.$("#measurement-checks").hidden = this.measurementGoal !== "cooldown" || !ready;
    if(ready) {
      this.$("#measurement-checks").innerHTML = `<h2>${ready.can_pause ? "Ready for a controlled cooldown" : "Before your first cooldown"}</h2><p>We check your actual HA readings. Fix the items below, then return here; these checks update automatically.</p>${ready.checks.filter(check=>!check.ready).map(check=>`<div style="padding:12px 0;border-bottom:1px solid var(--divider-color,#ccd8d6)"><strong>${check.ready ? "✓" : "○"} ${escapeHTML(check.title)}</strong><p>${escapeHTML(check.message)}</p>${check.action === "setup" ? '<button data-fix="setup">Choose heating control</button>' : check.action === "commission" ? `<button data-fix="${ops.source.commissioning_allowed ? "commission" : "setup"}">${ops.source.commissioning_allowed ? "Go to supervised check" : "Set up supervised check"}</button>` : check.action === "sensor" ? '<a href="/config/integrations/integration/thermal_planner">Review room sensors and freshness</a>' : check.action === "source" && check.entity_id ? `<button data-source="${escapeHTML(check.entity_id)}">Open heat pump control</button>` : ""}</div>`).join("")}<details style="margin-top:12px"><summary>View ${ready.checks.filter(check=>check.ready).length} ready checks</summary><ul>${ready.checks.filter(check=>check.ready).map(check=>`<li>${escapeHTML(check.title)} — ${escapeHTML(check.message)}</li>`).join("")}</ul></details>${!ready.checks.find(c=>c.title === "Pause and restore check")?.ready ? '<p>Use Set up measurement to allow the supervised check. Then start the one-minute check below while someone can observe the installation.</p>' : ""}`;
      this.shadowRoot.querySelectorAll("[data-fix]").forEach(b=>b.onclick=()=>{if(b.dataset.fix === "commission"){this.$("#onsite")?.scrollIntoView({block:"center",behavior:"smooth"});this.$("#onsite")?.focus();}else this.openMeasurementSetup();});
      this.shadowRoot.querySelectorAll("[data-source]").forEach(b=>b.onclick=()=>this.dispatchEvent(new CustomEvent("hass-more-info",{bubbles:true,composed:true,detail:{entityId:b.dataset.source}})));
    }
  }

  setMeasurementGoal(goal) { this.measurementGoal=goal;this.renderMeasurementEntry();this.labSignature=null;this.renderLab(); }

  openMeasurementSetup() {
    this.setupOpen=true;this.$("#measurement-context").hidden=true;this.message();
    const source=this.operations.source, root=this.$("#measurement-setup");root.hidden=false;
    const picker=(key,label,domain,selected,temperature=false)=>`<label for="setup-${key}">${label}</label><select id="setup-${key}"><option value="">${key === "climate" ? "No heat pump control — observation only" : "Not connected (optional)"}</option>${Object.entries(this._hass.states||{}).filter(([id,state])=>domain.includes(id.split(".")[0]) && (!temperature || ["°C","°F","K"].includes(state.attributes.unit_of_measurement))).map(([id,state])=>`<option value="${escapeHTML(id)}" ${id===selected?'selected':''}>${escapeHTML(state.attributes.friendly_name||id)} · ${escapeHTML(state.state)} ${escapeHTML(state.attributes.unit_of_measurement||"")} · ${escapeHTML(id)}</option>`).join("")}</select>`;
    root.innerHTML=`<h2>Connect your heating readings</h2><p>Choose the entities already in Home Assistant. Saving these choices sends no heating commands. Room sensors remain the air-temperature sensors you already added.</p>
      <details><summary>Help me match HeishaMon readings</summary><p>Save the JSON page from your HeishaMon as a file, then open it here. This is a one-time reference, not a new telemetry connection.</p><label for="heishamon-file">HeishaMon JSON file</label><input id="heishamon-file" type="file" accept=".json,application/json"><div id="heishamon-preview"></div><div class="scroll"><table><thead><tr><th>HeishaMon name</th><th>What to look for in HA</th></tr></thead><tbody><tr><td>Outside_Temp</td><td>Outdoor air temperature</td></tr><tr><td>Main_Outlet_Temp</td><td>Actual water leaving the pump</td></tr><tr><td>Main_Inlet_Temp</td><td>Returning water, for an optional zone reading</td></tr><tr><td>Main_Target_Temp</td><td>Requested water temperature, read only here</td></tr><tr><td>Operating_Mode_State</td><td>Heating versus hot water (DHW)</td></tr><tr><td>Defrosting_State</td><td>Defrost active indicator</td></tr><tr><td>Zones_State</td><td>Which pump zones are active</td></tr></tbody></table></div><p><strong>Heatpump_State is not proof that rooms are receiving heat.</strong> Pump_Flow is litres/minute, not a temperature. Z1/Z2 fields may exist even if only one zone is fitted. Keep your existing HA control integration; no MQTT setup is needed here.</p></details>
      <form id="measurement-setup-form">${picker("climate","1. Which control switches your space heating between Heat and Off?",["climate"],source.climate)}
      <p><small>Select the shared heating control, not the hot-water tank. If unsure, open the control in HA and check its modes. We do not guess this from the HeishaMon snapshot.</small></p>
      ${picker("outdoor","2. Outdoor air temperature",["sensor"],this.entry?.outdoor||this.operations.current?.outdoor_source,true)}
      <details><summary>Optional pump readings — add only what you recognise</summary>
      ${picker("operating_mode","Heating or hot-water mode",["sensor","select"],source.operating_mode)}
      <label for="setup-dhw_states">Which values mean hot water is being heated?</label><input id="setup-dhw_states" value="${escapeHTML((source.dhw_states||[]).join(","))}" placeholder="For example DHW, or the numeric value your HA sensor shows"><p><small>Use the value shown by the selected HA entity, not a number copied blindly from another installation.</small></p>
      ${picker("defrost_indicator","Defrost active indicator",["binary_sensor","sensor"],source.defrost_indicator)}
      ${picker("leaving_water","Actual water leaving the heat pump",["sensor"],source.leaving_water,true)}
      ${picker("flow_readback","Requested heating-water temperature (read only)",["sensor","number","input_number"],source.flow_readback,true)}</details>
      <label for="setup-age">How long may live readings go without being reported? (minutes)</label><input id="setup-age" type="number" min="1" max="1440" value="${this.entry?.max_age_minutes||30}" required><p><small>Live checks use HA's latest report time, even if the number did not change. Historical carry-forward is configured separately on Reports.</small></p>
      <label><input id="setup-supervised" type="checkbox" ${source.commissioning_allowed?'checked':''}> I want to enable the supervised pause/restore check. Someone will be present when I start it.</label>
      <p>This checkbox does not start the check or enable automatic heating.</p><button class="primary" type="submit">Save selected readings</button> <button id="cancel-setup" type="button">Cancel</button><p id="setup-error" role="alert" hidden></p></form>`;
    this.$("#cancel-setup").onclick=()=>{this.setupOpen=false;root.hidden=true;this.$("#measurement-context").hidden=false;};
    this.$("#heishamon-file").onchange=async event=>{
      try {
        const file=event.target.files[0];if(!file)return;if(file.size>1024*1024)throw new Error("Choose a JSON file smaller than 1 MB.");
        const raw=JSON.parse(await file.text());this.showHeishaMon(raw);
      } catch(error){this.$("#heishamon-preview").textContent=error.message||"That file could not be read. Choose the JSON status file.";}
    };
    this.$("#measurement-setup-form").onsubmit=async event=>{
      event.preventDefault();const values={};
      for(const key of ["climate","outdoor","operating_mode","defrost_indicator","leaving_water","flow_readback"])values[key]=this.$(`#setup-${key}`).value||null;
      values.dhw_states=this.$("#setup-dhw_states").value.split(",").map(s=>s.trim()).filter(Boolean);
      values.commissioning_allowed=this.$("#setup-supervised").checked;values.max_age_minutes=Number(this.$("#setup-age").value);
      const button=this.$("#measurement-setup-form button[type=submit]");button.disabled=true;
      try {this.operations=await this.call("operations",{entry_id:this.entryId,action:"setup",setup:values});this.labState=this.operations.control;this.setupOpen=false;root.hidden=true;this.$("#measurement-context").hidden=false;this.message("Readings saved. No heating commands were sent. Review the updated checks before starting.");this.labSignature=null;this.renderMeasurementEntry();this.renderSource();this.renderLab();}
      catch(error){const box=this.$("#setup-error");box.hidden=false;box.textContent=error.message||String(error);button.disabled=false;}
    };
    root.scrollIntoView({block:"start",behavior:"smooth"});
  }

  showHeishaMon(raw) {
    const rows=Object.values(raw||{}).filter(Array.isArray).flat().filter(r=>r&&typeof r.Name==="string");
    if(!rows.length)throw new Error("No HeishaMon status fields found. Your existing choices have not changed.");
    const fields={Outside_Temp:["outdoor","Outdoor air"],Main_Outlet_Temp:["leaving_water","Water leaving the pump"],Main_Target_Temp:["flow_readback","Requested water temperature"],Operating_Mode_State:["operating_mode","Space heating / hot water"],Defrosting_State:["defrost_indicator","Defrost indication"]};
    const normalize=s=>String(s).toLowerCase().replace(/[^a-z0-9]/g,"");let html='<h3>Match this snapshot to Home Assistant</h3><p>Suggestions below are based on names only. Review the entity and current HA value before saving. Nothing is connected automatically.</p><ul>';
    for(const row of rows){const field=fields[row.Name];if(!field)continue;const [key,label]=field;const select=this.$(`#setup-${key}`);
      const candidates=Array.from(select.options).filter(o=>o.value&&normalize(o.textContent).includes(normalize(row.Name)));
      html+=`<li><strong>${escapeHTML(label)}</strong>: ${escapeHTML(row.Name)} · ${escapeHTML(row.Value)} ${escapeHTML(row.Description)}${candidates.length===1?` <button type="button" data-suggestion="${key}" data-entity="${escapeHTML(candidates[0].value)}">Review suggested HA entity</button>`:' · Choose the matching HA entity above/below; names can differ.'}</li>`;
    }
    const zone=rows.find(r=>r.Name==="Zones_State"), mode=rows.find(r=>r.Name==="Operating_Mode_State");
    for(const number of [1,2]){const sensor=rows.find(r=>r.Name===`Z${number}_Sensor_Settings`);if(sensor && /water/i.test(sensor.Description))html+=`<li>Zone ${number} uses a water sensor in this snapshot. Use separate room-air sensors for your rooms.</li>`;}
    if(zone)html+=`<li>Zone status in snapshot: ${escapeHTML(zone.Description)}. This does not create or change zones.</li>`;
    if(mode)html+=`<li>Current operating mode in snapshot: ${escapeHTML(mode.Description)}. A hot-water cycle can be a reason to wait before a cooldown.</li>`;
    const invalid=rows.filter(r=>/Temp/.test(r.Name)&&[-78,-128].includes(Number(r.Value))).map(r=>r.Name);
    html+=`</ul>${invalid.length?`<p>Likely unused temperature channels (${escapeHTML(invalid.join(", "))}) show sentinel values. Do not use them as room air sensors.</p>`:""}<p>Heatpump_State does not identify an HA climate control. Room sensors must measure room air; Z1_Temp may represent water depending on Z1_Sensor_Settings.</p>`;
    this.$("#heishamon-preview").innerHTML=html;
    this.shadowRoot.querySelectorAll("[data-suggestion]").forEach(button=>button.onclick=()=>{const select=this.$(`#setup-${button.dataset.suggestion}`);select.value=button.dataset.entity;select.focus();button.textContent="Selected for review — save when checked";});
  }

  renderSource() {
    const source = this.operations?.source || this.entry?.heat_source || {};
    const rows = this.operations?.rooms || [];
    const friendly = this._hass.states?.[source.climate]?.attributes?.friendly_name || source.name || "Heat pump";
    const zones = source.zones || [{id:"main",name:"Main zone"}];
    const emitter = {radiator:"Radiators",underfloor:"Underfloor heating",mixed:"Mixed emitters",other:"Other heating"};
    this.$("#source").innerHTML = `<div class="row"><div><div class="eyebrow">One shared heat source</div><h2>${escapeHTML(friendly)}</h2></div><span class="badge">${source.climate ? "Configured heat pump" : "Observation only · no heat pump selected"}</span></div>
      <div class="legend">${zones.map(zone => `<span class="badge">${escapeHTML(zone.name)}${rows.length ? ` · ${escapeHTML([...new Set(rows.filter(room => (room.zone || zones[0].id) === zone.id).map(room=>emitter[room.emitter]||room.emitter))].join(" + "))}` : ""}</span>`).join("")}</div>
      <p><small>Zones belong to this heat pump. Radiators and UFH are room heating types and can share one zone. ${source.climate ? "Only the central configured source is commanded; zone entities provide context." : 'Choose a source in <a href="/config/integrations/integration/thermal_planner">Configure → Heat pump</a> when ready. Reports work without one.'}</small></p>`;
  }

  renderLab() {
    const state = this.labState, ops = this.operations;
    const root = this.$("#lab");
    root.hidden = !this._hass.user?.is_admin || !state || this.measurementGoal !== "cooldown";
    if (root.hidden) return;
    const signature = JSON.stringify([state, ops.observation, ops.baseline, ops.campaign, ops.mode, this.busy]);
    if (signature === this.labSignature) return;
    this.labSignature = signature;
    const source = ops.source, active = state.journal, run = active || state.last_run;
    const recovery = ops.observation;
    const baseline = ops.baseline;
    const campaign = ops.campaign;
    const status = ({idle:"Ready",paused:"Climate paused",prepared:"Preparing operation",resuming:"Resuming heat",recovering:"Observing at trial flow",restoring:"Restoring original settings",restore_failed:"Restore needs attention",waiting_for_entity:"Waiting for source/readback"})[state.status] || state.status;
    const limit = source.pause_limit_minutes || 30;
    const busy = this.busy ? "disabled" : "";
    const canStart = (ops.readiness?.can_pause ?? state.available) && !active && !recovery && !baseline && !campaign?.active && ops.mode === "advisory" && !this.busy;
    root.innerHTML = `<div class="row"><h2>Controlled measurements</h2><span class="badge">${state.simulated ? "Development simulator" : state.available ? "Commissioned source" : "Observation only"}</span></div>
      <p>Run a bounded cooldown, restore the original settings, then observe recovery. Minimum-temperature and missing-data guards stay active for every room.</p>
      <p><strong>${escapeHTML(!active && status === "Ready" && ops.readiness?.can_pause === false ? "Not ready to pause — review the checks above" : status)}</strong>${active ? ` · Restore deadline ${escapeHTML(this.time(active.recovery_deadline || active.deadline))}` : ""}${recovery ? ` · Recording recovery until ${escapeHTML(this.time(recovery.deadline))}` : ""}${baseline ? ` · Normal operation observation until ${escapeHTML(this.time(baseline.deadline))}` : ""}</p>
      ${active?.error ? `<p class="warning">${escapeHTML(active.error)}. Three restore attempts are allowed per retry sequence. Use Stop / retry restore after checking availability.</p>` : ""}
      ${!state.configured ? '<p>Choose the heat pump in Configure before starting measurements.</p>' : ""}
      ${state.configured && !state.available ? `<p>Pause/restore capability has not been commissioned for this source. Configuration alone does not enable control.</p>
        ${source.commissioning_allowed ? `<label><input class="compact" id="onsite" type="checkbox"> I am on site and have checked the central Heat/Off actions, DHW/schedule interaction and safe restoration.</label><button id="commission" disabled>Start supervised 1-minute commissioning</button>` : '<p><small>After checking the installation, enable supervised commissioning in Heat pump settings. Starting it is a separate explicit action here.</small></p>'}` : ""}
      <form id="lab-form"><div><label for="minutes">Maximum pause (real minutes)</label><input id="minutes" type="number" min="1" max="${limit}" step="1" value="${Math.min(5,limit)}" required ${active || recovery ? "disabled" : ""}></div>
        <button class="primary" type="submit" ${canStart ? "" : "disabled"}>Run once</button>
        <button id="stop" type="button" ${active || recovery || baseline || campaign?.active || ops.mode !== "advisory" ? busy : "disabled"}>Stop / retry restore</button>
        <button id="campaign" type="button" ${state.available && ops.campaign_settings && !campaign?.active && !this.busy ? "" : "disabled"}>Start campaign</button></form>
      ${source.flow_setter ? `<form id="flow-form" style="margin-top:16px"><div><label for="trial-flow">Approved direct-flow trial (°C)</label><input id="trial-flow" type="number" min="${source.flow_min}" max="${source.flow_max}" step="0.1" value="${ops.current?.flow_target ?? source.flow_min}" required></div><button type="submit" ${canStart ? "" : "disabled"}>Start flow trial</button></form><p><small>Explicit range ${source.flow_min}–${source.flow_max}°C; maximum change ${source.flow_max_step}°C. A trial owns mode and flow through cooldown/recovery, then restores both. An untested lower flow is not treated as a proven saving.</small></p>` : ""}
      ${campaign ? `<details ${campaign.active ? "open" : ""}><summary>Campaign · ${campaign.active ? "active" : "stopped"} · ${campaign.completed}/${campaign.settings.nights} completed</summary><p>Next local date: ${escapeHTML(campaign.next_date)}. Ends ${escapeHTML(campaign.settings.end_date)}.</p><ul>${campaign.skips.map(skip=>`<li>${escapeHTML(skip.date)}: ${escapeHTML(skip.reason)}</li>`).join("") || "<li>No skipped nights.</li>"}</ul></details>` : '<p><small>Configure nightly time, target count and end date under Campaign schedule before starting a campaign.</small></p>'}
      <p><small>Each pause is bounded by an absolute real-time deadline. HA cannot restore a device while HA is offline; interrupted ownership is reconciled on restart. Simulator temperatures can be accelerated.</small></p>
      ${run ? `<details ${active ? "open" : ""}><summary>${active ? "Active control journal" : "Last source operation"}</summary><p><small>${escapeHTML(run.reason || "Bounded measurement in progress.")}${run.outcome ? ` · ${escapeHTML(run.outcome)}` : ""}</small></p><ol class="events">${(run.events||[]).map(event=>`<li>${escapeHTML(this.time(event.at))} · ${escapeHTML(event.message)}</li>`).join("")}</ol></details>` : ""}`;
    this.$("#lab-form").addEventListener("submit", event=>{event.preventDefault();this.operation("run",{minutes:Number(this.$("#minutes").value)});});
    this.$("#stop").addEventListener("click",()=>this.operation("stop"));
    this.$("#campaign").addEventListener("click",()=>this.operation("campaign"));
    this.$("#onsite")?.addEventListener("change",()=>{this.$("#commission").disabled=!this.$("#onsite").checked || this.busy || ops.readiness?.can_commission===false;});
    this.$("#commission")?.addEventListener("click",()=>this.operation("commission",{acknowledged:this.$("#onsite").checked}));
    this.$("#flow-form")?.addEventListener("submit",event=>{event.preventDefault();this.operation("trial",{minutes:Number(this.$("#minutes").value),flow_target:Number(this.$("#trial-flow").value)});});

  }

  async operation(action, data={}) {
    if (this.busy) return;
    this.busy = true; this.renderMeasurementEntry(); this.renderLab(); this.renderOperations();
    this.message(({fit:"Fitting room models and checking holdout periods…",replan:"Fetching hourly forecasts and checking comfort margins…",run:"Saving the control journal and requesting a bounded pause…",trial:"Checking flow limits and saving original settings…",stop:"Stopping scheduled actions and reconciling owned settings…"})[action] || "Processing source operation…");
    try {
      this.operations = await this.call("operations",{entry_id:this.entryId,action,...data});
      this.labState = this.operations.control;
      this.message(action === "run" && this.labState?.status === "paused" ? "Bounded pause started. The journal and restore deadline are saved." : "Operation processed. Review its outcome and evidence below.");
    } catch(error) {this.message(error.message||String(error),true);await this.refresh(true);}
    finally {this.busy=false;this.renderSource();this.renderMeasurementEntry();this.renderLab();this.renderOperations();}
  }

  renderOperations() {
    const ops = this.operations;
    if (!ops) return;
    const signature = JSON.stringify([this.entryId,ops.models,ops.plan,ops.runs,ops.mode,ops.pending,ops.events,this.busy]);
    if(signature === this.operationsSignature)return;
    this.operationsSignature=signature;
    const admin = this._hass.user?.is_admin, busy = this.busy ? "disabled" : "";
    this.$("#runs").innerHTML = `<h2>Cooldown and recovery records</h2><p><small>Stored measurement points remain available independently of Recorder retention. Interrupted/incomplete recovery and energy coverage stay visible.</small></p>
      <div class="scroll"><table><thead><tr><th>Started</th><th>Flow °C</th><th>Recovery</th><th>Learning evidence</th><th>Timeline</th></tr></thead><tbody>${ops.runs.map(run=>`<tr><td>${escapeHTML(this.time(run.started))}</td><td>${number(run.flow_target,1)}</td><td>${run.recovery?.complete ? "Reached" : "Partial / not observed"}</td><td>${run.synthetic ? "Synthetic fixture" : run.learning_eligible ? "Eligible" : "Withheld"}</td><td><button data-run="${escapeHTML(run.id)}">View timeline</button></td></tr>`).join("") || '<tr><td colspan="5">No completed measurements yet.</td></tr>'}</tbody></table></div>`;
    this.shadowRoot.querySelectorAll("[data-run]").forEach(button=>button.addEventListener("click",()=>this.showRun(button.dataset.run)));
    const models = ops.models || {}, flows = models.flows || {};
    let modelRows="";
    for (const [key,group] of Object.entries(flows)) for (const [name,model] of Object.entries(group.rooms)) {
      const v=model.validation;
      modelRows+=`<tr><td>${escapeHTML(name)}</td><td>${group.flow_target == null ? "Not instrumented" : number(group.flow_target,1)}</td><td>${escapeHTML(model.state_kind)}</td><td>${group.episode_count}</td><td>${number(v.mae_c,3)}</td><td>${number(v.persistence_mae_c,3)}</td><td>${number(v.threshold_mae_hours,2)} h</td><td>${model.validated ? model.retained_previous ? "Previous fit retained" : "Validated" : "Withheld"}</td></tr>`;
    }
    this.$("#models").innerHTML=`<div class="row"><h2>Learning and validation</h2>${admin ? `<div><button id="fit" ${busy}>Fit room models</button> ${ops.control?.simulated ? `<button id="seed" ${busy}>Load development learning fixture</button>` : ""}</div>` : ""}</div>
      ${models.synthetic ? '<p class="warning">Synthetic learning fixture. These values are not installation evidence and cannot control a real heat pump.</p>' : ""}
      <p><small>Episodes are split chronologically into training, model-selection and untouched holdout groups. Heating/cooling error is compared with a persistence baseline. UFH can use an effective second heating-memory state. Physical room kWh and heat capacity are not inferred.</small></p>
      <div class="scroll"><table><thead><tr><th>Room</th><th>Flow °C</th><th>Response model</th><th>Episodes</th><th>Holdout error °C</th><th>Persistence error °C</th><th>Threshold error</th><th>Evidence</th></tr></thead><tbody>${modelRows || '<tr><td colspan="8">No validated fit yet. Collect complete cooldown and recovery episodes at the baseline target.</td></tr>'}</tbody></table></div>
      <ul>${(models.reasons||[]).map(reason=>`<li>${escapeHTML(reason)}</li>`).join("")}</ul>
      <details><summary>Validation horizons and source coverage</summary>${Object.values(flows).map(group=>Object.entries(group.rooms).map(([name,m])=>`<p><small>${escapeHTML(name)}: ${escapeHTML(Object.entries(m.validation.by_horizon_c).map(([h,error])=>`${h}: ${number(error,3)}°C`).join("; "))}. Outdoor range ${m.outside_range_c.map(v=>number(v,1)).join("–")}°C. ${m.input_kind ? escapeHTML(m.input_kind) : ""}</small></p>`).join("")).join("")}</details>
      <div id="demand"></div>`;
    this.$("#fit")?.addEventListener("click",()=>this.operation("fit"));
    this.$("#seed")?.addEventListener("click",()=>this.operation("seed_demo"));
    this.renderDemand(models.heat_demand);
    const plan=ops.plan;
    this.$("#planner").innerHTML=`<div class="row"><h2>Heating recommendations</h2><span class="badge">${escapeHTML(ops.mode === "advisory" ? "Advisory mode" : ops.mode === "auto" ? "Auto enabled" : "Apply once queued")}</span></div>
      ${admin ? `<div class="row"><div><button id="replan" ${busy}>Refresh forecast and plan</button> <button id="apply" ${plan?.automatic_eligible && ops.control?.available && !this.busy ? "" : "disabled"}>Apply once</button> <button id="auto" ${plan?.automatic_eligible && ops.control?.available && !this.busy ? "" : "disabled"}>Enable Auto</button></div><button id="disable" ${ops.mode === "advisory" || this.busy ? "disabled" : ""}>Stop automatic planning</button></div>` : ""}
      <p><strong>${plan?.status === "ready" ? "Supported candidate found" : "Recommendations withheld"}</strong></p><ul>${(plan?.reasons || [ops.forecast?.reason || "Fit validated models and refresh hourly forecasts to compare plans."]).map(reason=>`<li>${escapeHTML(reason)}</li>`).join("")}</ul>
      ${plan?.status === "ready" ? `<p>Horizon ${number(plan.horizon_hours,1)} h${plan.partial_forecast ? " (partial forecast)" : ""}. Heating on-time: ${number(plan.heating_hours,1)} h; baseline ${number(plan.baseline_heating_hours,1)} h. Ranking: ${escapeHTML(plan.ranking)}.</p>
      ${plan.estimated_electrical_kwh != null ? `<p>Estimated electrical input ${number(plan.estimated_electrical_kwh)} kWh; baseline ${number(plan.baseline_electrical_kwh)} kWh. These estimates use qualified measured space-heating input at represented conditions.</p>` : '<p><small>No supported electrical-energy estimate; on-time is a proxy, not a claimed kWh or cost saving.</small></p>'}
      <ul>${plan.actions.map(action=>`<li>${action.kind === "pause" ? `Pause ${escapeHTML(this.time(action.start))} → ${escapeHTML(this.time(action.end))}; restore the captured Heat setting.` : `Bounded ${number(action.target,1)}°C direct flow from ${escapeHTML(this.time(action.start))} → ${escapeHTML(this.time(action.end))}; restore the original flow.`}</li>`).join("") || '<li>Keep normal operation at the observed baseline flow.</li>'}</ul>
      <label for="prediction-view">Forecast comparison</label><select id="prediction-view" class="compact"><option value="selected">Selected plan</option><option value="baseline">Baseline operation</option></select><div id="predictions"></div>
      <p><small>Predictions include explicit validation uncertainty when checking targets. The long outlook extrapolates short-horizon validation. Fresh guards, weather, configuration and dwell limits are rechecked before any action.</small></p>` : ""}
      ${ops.events?.length ? `<details><summary>Recent operations</summary><ol class="events">${ops.events.map(event=>`<li>${escapeHTML(this.time(event.at))} · ${escapeHTML(event.message)}</li>`).join("")}</ol></details>` : ""}`;
    this.$("#replan")?.addEventListener("click",()=>this.operation("replan"));
    this.$("#apply")?.addEventListener("click",()=>this.operation("apply_once",{plan_id:plan.id}));
    this.$("#auto")?.addEventListener("click",()=>this.operation("auto",{plan_id:plan.id}));
    this.$("#disable")?.addEventListener("click",()=>this.operation("stop"));
    const plot = ()=>{if (plan?.times) this.$("#predictions").innerHTML=this.chart({start:plan.times[0],end:plan.times.at(-1),traces:{timestamps:plan.times,rooms:this.$("#prediction-view").value === "baseline" ? plan.baseline_predictions : plan.predictions}},true);};
    this.$("#prediction-view")?.addEventListener("change",plot);if(plan?.status === "ready")plot();
  }

  renderDemand(model) {
    const root=this.$("#demand");
    if(!model?.available){root.innerHTML=`<p><small>Whole-house heat demand: ${escapeHTML(model?.reason || "measured delivered space heat and stable covered weather windows are required.")}</small></p>`;return;}
    const current=this.operations.current, rooms=Object.values(current?.rooms||{}).filter(v=>typeof v==="number");
    const gap=current?.outdoor!=null && rooms.length ? rooms.reduce((a,b)=>a+b,0)/rooms.length-current.outdoor : 10;
    root.innerHTML=`<h3>Measured whole-house heat demand</h3><p>${number(model.w_per_k,1)} W/K · ${model.sample_count} stable windows · observed gap ${model.gap_range_k.map(v=>number(v,1)).join("–")} K. Residual spread ${number(model.rmse_w,0)} W.</p>
      <div class="row"><div><label for="demand-gap">Indoor–outdoor gap (K)</label><input id="demand-gap" class="compact" type="number" min="0" max="80" step="0.5" value="${number(gap,1)}"></div><div><label for="demand-hours">Duration (hours)</label><input id="demand-hours" class="compact" type="number" min="0.1" max="48" step="0.5" value="2"></div><p id="demand-value"></p></div><p><small>This is delivered thermal demand, not electrical consumption or room-level kWh.</small></p>`;
    const update=()=>{const g=Number(this.$("#demand-gap").value),h=Number(this.$("#demand-hours").value),kw=Math.max(0,model.w_per_k*g+model.intercept_w)/1000;this.$("#demand-value").textContent=`${number(kw)} kW thermal · ${number(kw*h)} kWh thermal${g<model.gap_range_k[0]||g>model.gap_range_k[1]?" · Extrapolated beyond observed conditions":""}`;};
    this.$("#demand-gap").addEventListener("input",update);this.$("#demand-hours").addEventListener("input",update);update();
  }

  async showRun(id) {
    try {
      const run=await this.call("operations",{entry_id:this.entryId,action:"run_detail",run_id:id});
      const points=run.points||[], names=Object.keys(points[0]?.rooms||{}), times=points.map(point=>new Date(point.time*1000).toISOString());
      const root=this.$("#run-detail");root.hidden=false;
      root.innerHTML=`<div class="row"><h2>${run.synthetic ? "Synthetic fixture" : "Measurement"} timeline</h2><button id="close-run">Close timeline</button></div><p>${escapeHTML(run.reason||"")}</p>
        <p><small>Cooldown ends ${run.cooldown_ended ? escapeHTML(this.time(run.cooldown_ended)) : "not recorded"}. Recovery ${run.recovery?.complete ? "reached all targets" : "partial or not observed"}.</small></p>
        ${times.length>1 ? this.chart({start:times[0],end:times.at(-1),traces:{timestamps:times,rooms:Object.fromEntries(names.map(name=>[name,points.map(point=>point.rooms[name])]))}}) : "<p>No covered samples.</p>"}
        ${this.energyHTML(run.energy)}<ul>${Object.entries(run.recovery?.rooms||{}).map(([name,row])=>`<li>${escapeHTML(name)}: target ${number(row.target_c,1)}°C; ${row.minutes==null ? "not reached" : `${number(row.minutes,1)} minutes`}; coverage ${percent(row.coverage)}.</li>`).join("")}</ul>`;
      this.$("#close-run").addEventListener("click",()=>{root.hidden=true;});root.scrollIntoView({behavior:"smooth"});
    }catch(error){this.message(error.message||String(error),true);}
  }

  energyHTML(energy) {
    if(!energy?.channels || !Object.keys(energy.channels).length)return "";
    const scopes={space_heating:"space heating only",heat_pump:"whole heat pump including DHW",wider:"wider/mixed circuit"};
    return `<h3>Energy channels</h3><ul>${Object.entries(energy.channels).map(([name,row])=>`<li>${name === "electrical" ? "Electrical input" : "Delivered thermal output"}: ${number(row.kwh)} kWh · ${escapeHTML(scopes[row.scope]||row.scope)} · coverage ${percent(row.coverage)}${row.partial ? " · Partial" : ""}${row.resets ? ` · ${row.resets} counter reset(s)` : ""}</li>`).join("")}</ul><p><small>COP: ${energy.cop==null ? "withheld — matched positive covered channels of compatible scope are required" : number(energy.cop)}.</small></p>`;
  }

  renderExtras(report) {
    let html=this.energyHTML(report.energy);
    const rows=Object.entries(report.rooms).filter(([,room])=>room.humidity||room.comfort_estimate);
    if(rows.length)html+=`<h3>Humidity and advisory comfort</h3><div class="scroll"><table><thead><tr><th>Room</th><th>End RH</th><th>Dew point °C</th><th>PMV index</th><th>PPD %</th></tr></thead><tbody>${rows.map(([name,room])=>`<tr><td>${escapeHTML(name)}</td><td>${number(room.humidity?.end_percent,1)}%</td><td>${number(room.humidity?.dew_point_c ?? room.comfort_estimate?.dew_point_c,1)}</td><td>${number(room.comfort_estimate?.pmv)}</td><td>${number(room.comfort_estimate?.ppd_percent,1)}</td></tr>`).join("")}</tbody></table></div>
      ${rows.filter(([,room])=>room.comfort_estimate).map(([name,room])=>{const a=room.comfort_estimate.assumptions;return `<p><small>${escapeHTML(name)} · ${escapeHTML(room.comfort_estimate.quality)}. Assumptions: activity ${number(a.met,1)} met; clothing ${number(a.clo,1)} clo; relative air speed ${number(a.relative_air_speed_m_s)} m/s; radiant temperature ${number(a.radiant_c,1)}°C (${escapeHTML(a.radiant_source)}). Measured air temperature drives guards; this comfort estimate does not control heating.</small></p>`;}).join("")}`;
    const diagnostics=report.traces.diagnostics||{};
    if(Object.keys(diagnostics).length)html+=`<h3>Extra recorded temperatures</h3>${this.chart({...report,traces:{timestamps:report.traces.timestamps,rooms:diagnostics}})}<p><small>These readings do not contribute to room/floor averages or control guards.</small></p>`;
    if(html){const div=document.createElement("div");div.className="card";div.innerHTML=html;this.$("#report").append(div);}
  }
}

customElements.define("thermal-planner-panel", ThermalPlannerPanel);
