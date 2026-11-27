/* Dell Warranty Sync — front-end controller.
 *
 * All long-running work happens in the Python backend (app/bridge.py) and
 * streams back as events through window.DWS.dispatch(). When the page is
 * opened in a plain browser (no pywebview), an in-page demo backend emits the
 * exact same events, so the rendering path is identical either way.
 */
(function () {
  "use strict";

  // ---- state ------------------------------------------------------------
  var State = {
    screen: "sync",
    filter: "all",
    dryRun: true,
    syncAll: false,
    phase: "idle", // idle | fetching | results | applying | done
    progress: 0,
    progressLabel: "",
    rows: [],
    today: "2026-07-02",
    field: "warranty_expiry_date_47",
    logLines: [],
    assetTypes: [
      { label: "Laptop", type_id: 47 },
      { label: "Desktop", type_id: 48 },
      { label: "Workstation", type_id: 51 },
      { label: "Server", type_id: 53 },
    ],
    conn: { dell: "ok", fs: "ok" },
    demoMode: true,
  };
  var toastTimer = null;

  var $ = function (id) { return document.getElementById(id); };
  var qs = function (sel, root) { return (root || document).querySelector(sel); };

  // ---- derivation (ported 1:1 from the design) --------------------------
  var BADGES = {
    "update":  { label: "Update",      bg: "#fff3d6", color: "#8a5a00", dot: "#e0a000" },
    "match":   { label: "In sync",     bg: "#e3f1ea", color: "#00703c", dot: "#009b48" },
    "synced":  { label: "Synced",      bg: "#cdeadd", color: "#005c31", dot: "#007f3b" },
    "no-data": { label: "No data",     bg: "#eaeef0", color: "#4c6272", dot: "#8a99a1" },
    "no-tag":  { label: "No tag",      bg: "#eaeef0", color: "#4c6272", dot: "#8a99a1" },
    "bad-tag": { label: "Invalid tag", bg: "#ffe7cc", color: "#8a4b00", dot: "#ed8b00" },
    "error":   { label: "Error",       bg: "#f9dedb", color: "#8f1a10", dot: "#d5281b" },
  };
  function badge(st) { return BADGES[st] || BADGES["no-data"]; }

  function daysInfo(iso) {
    if (!iso) return null;
    var d = new Date(iso + "T00:00:00");
    var today = new Date(State.today + "T00:00:00");
    var days = Math.round((d - today) / 86400000);
    var color = days < 0 ? "#c62f22" : days < 90 ? "#c47f00" : "#4c6272";
    return { days: days, color: color };
  }

  function matchFilter(status, f) {
    if (f === "all") return true;
    if (f === "match") return status === "match" || status === "synced";
    if (f === "no-data") return status === "no-data" || status === "no-tag";
    return status === f;
  }

  function logColor(text) {
    if (/FAIL|ERROR|! /.test(text)) return "#f8a094";
    if (/WARNING/.test(text)) return "#f4c14e";
    if (/ \+ /.test(text) || /: OK|OK:/.test(text) || /\+ filter/.test(text) || /confirmed/.test(text) || /complete/.test(text)) return "#7fe0a6";
    if (/DRY RUN|  ~ /.test(text)) return "#8fc3f5";
    if (/Querying Dell|Fetching|Detecting|Probing/.test(text)) return "#cfe0f0";
    return "#aebfce";
  }

  function isBusy() { return State.phase === "fetching" || State.phase === "applying"; }
  function pendingCount() {
    return State.rows.filter(function (r) { return r.status === "update"; }).length;
  }

  // ---- backend bridge ---------------------------------------------------
  function backend() {
    return (window.pywebview && window.pywebview.api) ? window.pywebview.api : DemoBackend;
  }
  function call(method) {
    var args = Array.prototype.slice.call(arguments, 1);
    var api = backend();
    try {
      var r = api[method].apply(api, args);
      return (r && typeof r.then === "function") ? r : Promise.resolve(r);
    } catch (e) {
      return Promise.reject(e);
    }
  }

  // ---- navigation -------------------------------------------------------
  function showScreen(name) {
    State.screen = name;
    var sections = document.querySelectorAll(".screen");
    for (var i = 0; i < sections.length; i++) {
      sections[i].classList.toggle("active", sections[i].getAttribute("data-screen") === name);
    }
    var navs = document.querySelectorAll(".nav-item");
    for (var j = 0; j < navs.length; j++) {
      navs[j].classList.toggle("active", navs[j].getAttribute("data-nav") === name);
    }
    if (name === "log") renderLog();
  }

  function updateDemoBadge() {
    $("demo-badge").style.display = State.demoMode ? "inline-block" : "none";
  }

  function updateLogBadge() {
    var badgeEl = $("log-badge");
    badgeEl.textContent = String(State.logLines.length);
    badgeEl.style.opacity = State.logLines.length > 0 ? "1" : "0";
  }

  // ---- sync screen ------------------------------------------------------
  function updateToggle(id, on) {
    $(id).classList.toggle("on", on);
  }

  function updateFetchButton() {
    var btn = $("fetch-btn");
    btn.classList.toggle("busy", isBusy());
    $("fetch-label").textContent =
      (isBusy() && State.phase === "fetching") ? "Fetching…" : "Fetch assets & query Dell";
    $("fetch-icon").style.animation = State.phase === "fetching" ? "spin .8s linear infinite" : "none";
  }

  function updateApplyButton() {
    var ready = pendingCount() > 0 && !isBusy();
    $("apply-btn").classList.toggle("ready", ready);
  }

  function renderSyncBody() {
    $("progress-card").style.display = isBusy() ? "block" : "none";
    $("empty-state").style.display = State.phase === "idle" ? "flex" : "none";
    $("results").style.display = State.rows.length > 0 ? "block" : "none";
  }

  function updateProgress() {
    $("progress-fill").style.width = State.progress + "%";
    $("progress-pct").textContent = State.progress + "%";
    $("progress-label").textContent = State.progressLabel;
  }

  var FILTER_LABELS = {
    all: "All assets", update: "Pending updates", match: "In sync",
    "no-data": "No Dell data", "bad-tag": "Invalid tag", error: "Errors",
  };
  var CARD_DEFS = [
    { key: "all", label: "Total assets", tone: "#005EB8" },
    { key: "update", label: "Pending updates", tone: "#c47f00" },
    { key: "match", label: "In sync", tone: "#007f3b" },
    { key: "no-data", label: "No Dell data", tone: "#5a7080" },
    { key: "bad-tag", label: "Invalid tag", tone: "#c46a00" },
    { key: "error", label: "Errors", tone: "#c62f22" },
  ];

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function renderCards() {
    var wrap = $("cards");
    wrap.innerHTML = "";
    CARD_DEFS.forEach(function (c) {
      var active = State.filter === c.key;
      var count = State.rows.filter(function (r) { return matchFilter(r.status, c.key); }).length;
      var card = el("button", "summary-card");
      card.style.borderColor = active ? c.tone : "#e6ebee";
      card.style.background = active ? "#f6fafd" : "#ffffff";
      var top = el("div", "top");
      var dot = el("span", "dot"); dot.style.background = c.tone;
      top.appendChild(dot);
      top.appendChild(el("span", "lbl", c.label));
      var num = el("div", "count", String(count)); num.style.color = c.tone;
      card.appendChild(top);
      card.appendChild(num);
      card.addEventListener("click", function () { setFilter(c.key); });
      wrap.appendChild(card);
    });
  }

  function renderRows() {
    var tbody = $("rows");
    tbody.innerHTML = "";
    var shown = 0;
    State.rows.forEach(function (r) {
      if (!matchFilter(r.status, State.filter)) return;
      shown++;
      var b = badge(r.status);
      var dd = daysInfo(r.dellEnd);
      var showDetail = r.detail && r.status !== "update" && r.status !== "match" && r.status !== "synced";
      var tr = el("tr");

      var tdAsset = el("td");
      tdAsset.appendChild(el("div", "asset-name", r.name));
      if (showDetail) tdAsset.appendChild(el("div", "asset-detail", r.detail));
      tr.appendChild(tdAsset);

      tr.appendChild(el("td", "cell-muted", r.type || "—"));
      tr.appendChild(el("td", "cell-tag", r.tag || "—"));

      var tdFs = el("td", "cell-date", r.fsEnd || "—"); tdFs.style.color = "#4c6272";
      tr.appendChild(tdFs);
      var tdDell = el("td", "cell-date", r.dellEnd || "—"); tdDell.style.color = "#212b32";
      tr.appendChild(tdDell);

      var tdDays = el("td", "cell-date num", dd ? String(dd.days) : "—");
      tdDays.style.color = dd ? dd.color : "#8a99a1";
      tdDays.style.fontWeight = "500";
      tr.appendChild(tdDays);

      tr.appendChild(el("td", "cell-level", r.level || "—"));
      tr.appendChild(el("td", "cell-muted", r.system || "—"));

      var tdStatus = el("td");
      var span = el("span", "badge");
      span.style.background = b.bg; span.style.color = b.color;
      var bdot = el("span", "dot"); bdot.style.background = b.dot;
      span.appendChild(bdot);
      span.appendChild(document.createTextNode(b.label));
      tdStatus.appendChild(span);
      tr.appendChild(tdStatus);

      tbody.appendChild(tr);
    });
    $("shown-count").textContent = String(shown);
    $("total-count").textContent = String(State.rows.length);
    $("filter-label").textContent = FILTER_LABELS[State.filter] || "All assets";
    $("warranty-field").textContent = State.field;
  }

  function renderResults() {
    if (State.rows.length === 0) return;
    renderCards();
    renderRows();
  }

  function setFilter(f) {
    State.filter = f;
    renderResults();
  }

  // ---- log --------------------------------------------------------------
  function renderLog() {
    var term = $("terminal");
    term.innerHTML = "";
    if (State.logLines.length === 0) {
      var empty = el("div", "empty-line",
        "No activity yet. Run a sync from the Warranty Sync tab to see live output here.");
      term.appendChild(empty);
      return;
    }
    State.logLines.forEach(function (t) {
      var line = el("div", "line", t);
      line.style.color = logColor(t);
      term.appendChild(line);
    });
    term.scrollTop = term.scrollHeight;
  }

  function appendLogLine(text) {
    State.logLines.push(text);
    updateLogBadge();
    if (State.screen === "log") renderLog();
  }

  // ---- toast ------------------------------------------------------------
  function showToast(msg, dot) {
    $("toast-text").textContent = msg;
    $("toast-dot").style.background = dot || "#45d17f";
    $("toast").classList.add("show");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { $("toast").classList.remove("show"); }, 3400);
  }

  // ---- connection pills -------------------------------------------------
  function updatePill(which) {
    var pill = $(which === "dell" ? "dell-pill" : "fs-pill");
    var st = State.conn[which];
    pill.className = "pill" + (st === "testing" ? " testing" : st === "error" ? " error" : "");
    var label = st === "testing" ? "Testing…" : st === "error" ? "Error" : "Authenticated";
    pill.innerHTML = "";
    pill.appendChild(el("span", "dot"));
    pill.appendChild(document.createTextNode(label));
  }

  // ---- event sink (Python → page) ---------------------------------------
  window.DWS = {
    dispatch: function (ev) {
      switch (ev.type) {
        case "phase":
          State.phase = ev.phase;
          if (ev.phase === "fetching") { State.rows = []; State.filter = "all"; renderResults(); }
          renderSyncBody(); updateFetchButton(); updateApplyButton();
          break;
        case "progress":
          State.progress = ev.value; State.progressLabel = ev.label; updateProgress();
          break;
        case "log":
          appendLogLine(ev.text);
          break;
        case "rows":
          State.rows = ev.rows || [];
          State.today = ev.today || State.today;
          State.field = ev.field || State.field;
          State.filter = "all";
          renderSyncBody(); renderResults(); updateApplyButton();
          break;
        case "row_update":
          var r = State.rows.filter(function (x) { return x.id === ev.id; })[0];
          if (r) { r.status = ev.status; r.detail = ev.detail; }
          renderResults(); updateApplyButton();
          break;
        case "toast":
          showToast(ev.msg, ev.dot);
          break;
        case "conn":
          State.conn[ev.which] = ev.status; updatePill(ev.which);
          break;
      }
    },
  };

  // ---- actions ----------------------------------------------------------
  function selectedAssetType() {
    var idx = $("asset-type").selectedIndex;
    return State.assetTypes[idx] || State.assetTypes[0];
  }

  var ACTIONS = {
    "win-min": function () { call("minimize"); },
    "win-max": function () { call("toggle_maximize"); },
    "win-close": function () { call("close_window"); },

    "toggle-dryrun": function () { State.dryRun = !State.dryRun; updateToggle("dry-toggle", State.dryRun); },
    "toggle-syncall": function () { State.syncAll = !State.syncAll; updateToggle("all-toggle", State.syncAll); },
    "toggle-live": function () {
      State.demoMode = !State.demoMode;
      updateToggle("live-toggle", !State.demoMode);
      updateDemoBadge();
    },

    "fetch": function () {
      if (isBusy()) return;
      var at = selectedAssetType();
      call("start_fetch", at.label, at.type_id, State.syncAll);
    },
    "apply": function () {
      if (isBusy()) return;
      call("start_apply", State.dryRun);
    },
    "export": function () { call("export_rows"); },

    "test-dell": function () { call("test_dell"); },
    "test-fs": function () { call("test_fs"); },
    "autodetect": function () {
      var at = selectedAssetType();
      call("autodetect", at.label, at.type_id);
    },
    "save": function () { call("save_config", gatherConfig()); },
    "reload": function () { call("reload_config").then(applyConfig); },
    "clear-log": function () {
      call("clear_log"); State.logLines = []; updateLogBadge(); renderLog();
    },
    "save-log": function () { call("save_log"); },
  };

  // ---- configuration ----------------------------------------------------
  function applyConfig(cfg) {
    if (!cfg) return;
    State.config = cfg;
    if (cfg.asset_types && cfg.asset_types.length) State.assetTypes = cfg.asset_types;
    // asset-type dropdown
    var sel = $("asset-type");
    sel.innerHTML = "";
    State.assetTypes.forEach(function (a) {
      sel.appendChild(el("option", null, a.label + " (id=" + a.type_id + ")"));
    });
    // dell + fs fields
    $("cfg-dell-id").value = cfg.dell_client_id || "";
    $("cfg-dell-auth").value = cfg.dell_auth_url || "";
    $("cfg-dell-warranty").value = cfg.dell_warranty_url || "";
    $("cfg-fs-domain").value = cfg.fs_domain || "";
    if (cfg.dell_secret_set) $("cfg-dell-secret").value = cfg.mask || "";
    if (cfg.fs_api_key_set) $("cfg-fs-key").value = cfg.mask || "";
    // field mapping
    var fm = cfg.field_map || {};
    Object.keys(fm).forEach(function (k) {
      var inp = $("map-" + k);
      if (inp) inp.value = fm[k];
    });
    // data source
    State.demoMode = cfg.demo_mode !== false;
    updateToggle("live-toggle", !State.demoMode);
    updateDemoBadge();
  }

  function gatherConfig() {
    var fm = {};
    ["warranty_expiry_date", "service_level", "system_description", "ship_date",
      "warranty_start_date", "end_of_life"].forEach(function (k) {
      var inp = $("map-" + k);
      if (inp) fm[k] = inp.value;
    });
    return {
      dell_client_id: $("cfg-dell-id").value,
      dell_auth_url: $("cfg-dell-auth").value,
      dell_warranty_url: $("cfg-dell-warranty").value,
      dell_client_secret: $("cfg-dell-secret").value,
      fs_domain: $("cfg-fs-domain").value,
      fs_api_key: $("cfg-fs-key").value,
      field_map: fm,
      demo_mode: State.demoMode,
    };
  }

  // ---- wiring -----------------------------------------------------------
  function wire() {
    document.addEventListener("click", function (e) {
      var nav = e.target.closest ? e.target.closest("[data-nav]") : null;
      if (nav) { showScreen(nav.getAttribute("data-nav")); return; }
      var act = e.target.closest ? e.target.closest("[data-action]") : null;
      if (act) {
        var name = act.getAttribute("data-action");
        if (ACTIONS[name]) ACTIONS[name]();
      }
    });
    updateToggle("dry-toggle", State.dryRun);
    updateToggle("all-toggle", State.syncAll);
    updateToggle("live-toggle", !State.demoMode);
    updateDemoBadge();
    updateLogBadge();
    updateFetchButton();
    updateApplyButton();
    renderSyncBody();
    updatePill("dell");
    updatePill("fs");
  }

  function loadConfig() {
    call("load_config").then(applyConfig).catch(function () { applyConfig(DemoBackend.config); });
  }

  function start() {
    wire();
    if (window.pywebview && window.pywebview.api) {
      loadConfig();
    } else {
      window.addEventListener("pywebviewready", loadConfig);
      // No pywebview at all (plain browser): fall back to the in-page demo.
      setTimeout(function () { if (!window.pywebview) applyConfig(DemoBackend.config); }, 60);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }

  // =======================================================================
  // In-page demo backend — only used when pywebview is absent. Mirrors the
  // Python demo (app/sync.py) so a browser preview behaves like the app.
  // =======================================================================
  var DEMO_ROWS = [
    { id: 1, name: "CORP-CLN-04821", type: "Laptop", tag: "7KJ2QM3", fsEnd: "2024-11-02", dellEnd: "2026-11-02", level: "ProSupport Plus: Next Business Day", system: "Latitude 5540", status: "update", detail: "" },
    { id: 2, name: "CORP-CLN-04822", type: "Laptop", tag: "9XR4TB1", fsEnd: "2026-03-15", dellEnd: "2026-03-15", level: "ProSupport: Next Business Day", system: "Latitude 5450", status: "match", detail: "" },
    { id: 3, name: "CORP-WRD-01193", type: "Desktop", tag: "3H7NLP2", fsEnd: "", dellEnd: "2027-06-30", level: "Basic Onsite Service", system: "OptiPlex 7010", status: "update", detail: "" },
    { id: 4, name: "CORP-LAB-00874", type: "Workstation", tag: "QJ8ZK5T", fsEnd: "2025-09-01", dellEnd: "2025-09-01", level: "ProSupport Plus: 4-Hour Mission Critical", system: "Precision 3591", status: "match", detail: "" },
    { id: 5, name: "CORP-CLN-04980", type: "Laptop", tag: "5MW3RD9", fsEnd: "2023-01-20", dellEnd: "2025-01-20", level: "ProSupport: Next Business Day", system: "Latitude 5420", status: "update", detail: "" },
    { id: 6, name: "CORP-THE-00233", type: "Desktop", tag: "8BFK2N6", fsEnd: "", dellEnd: "", level: "", system: "", status: "no-data", detail: "Dell returned no entitlement data for this tag" },
    { id: 7, name: "CORP-CLN-05001", type: "Laptop", tag: "ABC12", fsEnd: "", dellEnd: "", level: "", system: "", status: "bad-tag", detail: "'ABC12' is 5 characters; Dell tags are exactly 7 — skipped" },
    { id: 8, name: "CORP-ADM-00612", type: "Desktop", tag: "", fsEnd: "2024-05-05", dellEnd: "", level: "", system: "", status: "no-tag", detail: "No serial number on asset" },
    { id: 9, name: "CORP-CLN-04710", type: "Laptop", tag: "2QT9WX4", fsEnd: "2025-12-11", dellEnd: "2027-12-11", level: "ProSupport Plus: Next Business Day", system: "Latitude 7450", status: "update", detail: "" },
    { id: 10, name: "CORP-RAD-00088", type: "Workstation", tag: "6LP1VH8", fsEnd: "2026-08-19", dellEnd: "2026-08-19", level: "ProSupport: 4-Hour Mission Critical", system: "Precision 5860", status: "match", detail: "" },
    { id: 11, name: "CORP-CLN-05130", type: "Laptop", tag: "4NK7DM2", fsEnd: "", dellEnd: "2028-02-28", level: "ProSupport Plus: Next Business Day", system: "Latitude 5550", status: "update", detail: "" },
    { id: 12, name: "CORP-PHR-00341", type: "Desktop", tag: "7GW2XR5", fsEnd: "2022-10-03", dellEnd: "2024-10-03", level: "Basic Hardware Service", system: "OptiPlex 5090", status: "update", detail: "" },
    { id: 13, name: "CORP-CLN-04655", type: "Laptop", tag: "9DZ3PQ7", fsEnd: "2027-04-14", dellEnd: "2027-04-14", level: "ProSupport: Next Business Day", system: "Latitude 5340", status: "match", detail: "" },
    { id: 14, name: "CORP-ITS-00019", type: "Server", tag: "3XV8KB1", fsEnd: "2023-07-22", dellEnd: "2026-07-22", level: "ProSupport Plus: 4-Hour Mission Critical", system: "PowerEdge R650", status: "update", detail: "" },
    { id: 15, name: "CORP-CLN-05201", type: "Laptop", tag: "8HR4NT6", fsEnd: "", dellEnd: "2027-01-30", level: "ProSupport: Next Business Day", system: "Latitude 5531", status: "error", detail: "HTTP 400: field 'warranty_expiry_date_47' rejected by Freshservice" },
    { id: 16, name: "CORP-CLN-04533", type: "Laptop", tag: "5TW9LK3", fsEnd: "2026-01-30", dellEnd: "2026-01-30", level: "ProSupport: Next Business Day", system: "Latitude 5531", status: "match", detail: "" },
  ];
  var DEMO_SEQ = [
    [0, 8, "Connecting to Freshservice…", "Fetching %SCOPE% from Freshservice (id=47)..."],
    [550, 22, "Probing filter strategy…", "Probing filter strategy: filter=asset_type_id (id)"],
    [400, 33, "Paginating assets…", "  + filter=asset_type_id confirmed: page 1 has 100 assets, all matching"],
    [500, 46, "Paginating assets…", "Page 1: 100 returned, 100 kept"],
    [400, 56, "Paginating assets…", "Page 2: 62 returned, 62 kept"],
    [350, 63, "Assets fetched", "Fetched 16 assets (14 with serial numbers, 2 without)."],
    [400, 71, "Querying Dell TechDirect…", "Querying Dell for 13 service tags (7-char Dell format)"],
    [300, 75, "Querying Dell TechDirect…", "  Skipping 1 asset with a serial that isn't 7 characters."],
    [550, 88, "Querying Dell batch 1/1…", "Querying Dell batch 1/1 (13 tags)"],
    [550, 96, "Detecting warranty field…", "Warranty field detected: 'warranty_expiry_date_47'"],
    [350, 100, "Comparison complete", "Comparison complete: 7 update(s), 5 already in sync."],
  ];
  var demoTimers = [];
  function demoClear() { demoTimers.forEach(clearTimeout); demoTimers = []; }
  function demoAt(delay, fn) { demoTimers.push(setTimeout(fn, delay)); }
  function D(ev) { window.DWS.dispatch(ev); }

  var DemoBackend = {
    config: {
      dell_client_id: "a7f3c9e2-8b14-4d6a-9c02-1e5f8b7a2d90",
      dell_auth_url: "https://apigtwb2c.us.dell.com/auth/oauth/v2/token",
      dell_warranty_url: "https://apigtwb2c.us.dell.com/PROD/sbil/eapi/v5/asset-entitlements",
      dell_secret_set: true,
      fs_domain: "yourcompany.freshservice.com",
      fs_api_key_set: true,
      field_map: {
        warranty_expiry_date: "warranty_expiry_date_{type_id}",
        service_level: "support_tier_{type_id}",
        system_description: "product_model_{type_id}",
        ship_date: "ship_date_{type_id}",
        warranty_start_date: "",
        end_of_life: "end_of_life",
      },
      asset_types: [
        { label: "Laptop", type_id: 47 }, { label: "Desktop", type_id: 48 },
        { label: "Workstation", type_id: 51 }, { label: "Server", type_id: 53 },
      ],
      demo_mode: true,
      mask: "••••••••••••",
    },
    _rows: [],
    load_config: function () { return Promise.resolve(this.config); },
    minimize: function () {}, toggle_maximize: function () {}, close_window: function () {},

    start_fetch: function (label, typeId, syncAll) {
      demoClear();
      D({ type: "phase", phase: "fetching" });
      D({ type: "progress", value: 0, label: "Connecting to Freshservice…" });
      var scope = syncAll ? "ALL asset types" : "'Laptop' assets";
      var acc = 0, self = this;
      DEMO_SEQ.forEach(function (step) {
        acc += step[0];
        demoAt(acc, function () {
          D({ type: "progress", value: step[1], label: step[2] });
          D({ type: "log", text: step[3].replace("%SCOPE%", scope) });
        });
      });
      demoAt(acc + 250, function () {
        self._rows = DEMO_ROWS.map(function (r) { return Object.assign({}, r); });
        D({ type: "rows", rows: self._rows, today: "2026-07-02", field: "warranty_expiry_date_47" });
        D({ type: "phase", phase: "results" });
        D({ type: "toast", msg: "Comparison ready — 7 assets need updating.", dot: "#0072CE" });
      });
      return true;
    },

    start_apply: function (dryRun) {
      var pending = this._rows.filter(function (r) { return r.status === "update"; });
      if (pending.length === 0) {
        D({ type: "toast", msg: "Nothing to apply — everything is already in sync.", dot: "#8a99a1" });
        return true;
      }
      if (dryRun) {
        D({ type: "log", text: "DRY RUN: would update " + pending.length + " assets:" });
        pending.forEach(function (r) {
          D({ type: "log", text: "  ~ " + r.name + " (" + r.tag + "): " + (r.fsEnd || "(empty)") + " -> " + r.dellEnd });
        });
        D({ type: "toast", msg: "Dry run: " + pending.length + " assets would be updated. Untick Dry run to write.", dot: "#e0a000" });
        return true;
      }
      demoClear();
      D({ type: "phase", phase: "applying" });
      D({ type: "progress", value: 0, label: "Writing " + pending.length + " assets to Freshservice…" });
      D({ type: "log", text: "Applying " + pending.length + " updates with 6 field mapping(s)" });
      var total = pending.length;
      pending.forEach(function (r, i) {
        demoAt((i + 1) * 380, function () {
          r.status = "synced"; r.detail = "Updated 6 field(s)";
          D({ type: "row_update", id: r.id, status: "synced", detail: "Updated 6 field(s)" });
          D({ type: "progress", value: Math.round((i + 1) / total * 100), label: "Writing to Freshservice… (" + (i + 1) + "/" + total + ")" });
          D({ type: "log", text: "  + " + r.name + " (" + r.tag + "): 6 field(s) updated" });
          if (i === total - 1) {
            demoAt(300, function () {
              D({ type: "phase", phase: "done" });
              D({ type: "log", text: "Sync complete: " + total + " updated, 0 failed." });
              D({ type: "toast", msg: "Sync complete — " + total + " assets updated.", dot: "#45d17f" });
            });
          }
        });
      });
      return true;
    },

    export_rows: function () {
      if (this._rows.length === 0) {
        D({ type: "toast", msg: "Nothing to export — run a comparison first.", dot: "#8a99a1" });
        return null;
      }
      var n = this._rows.length;
      D({ type: "log", text: "Exported " + n + " rows to dell_warranty_sync.xlsx" });
      D({ type: "toast", msg: "Exported " + n + " rows to XLSX.", dot: "#45d17f" });
      return "dell_warranty_sync.xlsx";
    },

    test_dell: function () {
      D({ type: "conn", which: "dell", status: "testing" });
      demoAt(850, function () {
        D({ type: "conn", which: "dell", status: "ok" });
        D({ type: "log", text: "OK: Dell authentication successful." });
        D({ type: "toast", msg: "Dell TechDirect authenticated.", dot: "#45d17f" });
      });
      return true;
    },
    test_fs: function () {
      D({ type: "conn", which: "fs", status: "testing" });
      demoAt(850, function () {
        D({ type: "conn", which: "fs", status: "ok" });
        D({ type: "log", text: "OK: Freshservice authentication successful." });
        D({ type: "toast", msg: "Freshservice authenticated.", dot: "#45d17f" });
      });
      return true;
    },
    autodetect: function (label, typeId) {
      D({ type: "log", text: "Auto-detecting warranty field for '" + label + "' (id=" + typeId + ")..." });
      D({ type: "log", text: "  Detected warranty field from type definition: 'warranty_expiry_date_" + typeId + "'" });
      D({ type: "toast", msg: "Detected: warranty_expiry_date_" + typeId, dot: "#0072CE" });
      return true;
    },
    save_config: function () {
      D({ type: "log", text: "Settings saved to Credential Manager." });
      D({ type: "toast", msg: "Settings saved to Windows Credential Manager.", dot: "#45d17f" });
      return true;
    },
    reload_config: function () {
      D({ type: "log", text: "Settings reloaded from Credential Manager." });
      D({ type: "toast", msg: "Settings reloaded from store.", dot: "#0072CE" });
      return Promise.resolve(this.config);
    },
    clear_log: function () { return true; },
    save_log: function () {
      D({ type: "toast", msg: "Log saved to warranty_sync.log", dot: "#45d17f" });
      return "warranty_sync.log";
    },
  };
})();
