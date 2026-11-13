/* Tube Display screen editor - plain JS, no build step, relative URLs only
   (the page is served under the HA ingress X-Ingress-Path prefix). */
"use strict";

// Order = the type <select>: graph sits next to sparkline, the new types before custom/blank
const WIDGET_TYPES = ["text", "big_number", "ring", "bar", "icon", "image", "sparkline", "graph",
  "progress", "analog_clock", "weather", "ticker", "gif", "custom", "blank"];
// fields per widget type, mirroring render._WIDGET_TEMPLATE_FIELDS plus the
// other keys screens.py / render.py read
const FIELDS = {
  text:       ["text", "size", "align", "weight", "font", "color"],
  big_number: ["value", "unit", "label", "unit_newline", "font", "color"],
  ring:       ["value", "max", "label", "unit", "unit_newline", "color"],
  bar:        ["value", "max", "label", "unit", "color"],
  icon:       ["icon", "text", "color"],
  image:      ["source"],
  gif:        ["source", "loop"],
  sparkline:  ["entity", "hours", "unit", "color"],
  graph:      ["entity", "hours", "style", "label", "unit", "show_value", "color"],
  progress:   ["value", "max", "label", "finish", "eta_format", "color", "track"],
  analog_clock: ["tz", "seconds", "numerals", "label", "color", "accent", "face"],
  weather:    ["entity", "label", "show", "color", "icon_color"],
  ticker:     ["text", "size", "color", "background", "y", "speed", "label"],
  custom:     [],
  blank:      [],
};
// less used fields, behind "More options"
const MORE = {
  text:       ["valign"],
  big_number: ["value_size", "unit_size", "label_size", "value_color", "unit_color", "label_color"],
  ring:       ["radius", "thickness", "value_size", "unit_size", "label_size", "value_color", "label_color", "font"],
  bar:        ["value_size", "label_size", "value_color", "label_color", "font"],
  icon:       ["icon_size", "text_size", "text_color", "font"],
  gif:        ["background", "x", "y"],
  graph:      ["min", "max", "points", "value_size", "value_color", "label_color", "font"],
  progress:   ["value_size", "value_color", "label_size", "label_color", "eta_color", "font"],
  analog_clock: ["label_color", "font"],
  weather:    ["temp_size", "hl_color", "label_color", "font"],
  ticker:     ["label_color", "font"],
  sparkline:  ["font"],
};
// what a custom tube is made of (render.ITEM_TYPES) and each item's fields
const ITEM_FIELDS = {
  text:  ["text", "x", "y", "size", "weight", "align", "font", "color", "width", "height"],
  ring:  ["value", "max", "x", "y", "radius", "thickness", "color", "track", "start", "sweep"],
  bar:   ["value", "max", "x", "y", "width", "height", "direction", "color", "track"],
  icon:  ["icon", "x", "y", "size", "color"],
  image: ["source", "x", "y", "width", "height"],
  rect:  ["x", "y", "width", "height", "radius", "color"],
};
// new-item defaults; on a canvas (cw > TUBE_W) they are placed in strip coordinates
function itemNew(t, cw) {
  const wide = cw > TUBE_W, cx = Math.round(cw / 2);
  const base = {
    text:  { type: "text", text: "Text", x: cx, y: 120, size: 20 },
    ring:  { type: "ring", value: "50", max: 100, x: cx, y: 70, radius: 52, thickness: 10 },
    bar:   { type: "bar", value: "50", max: 100, x: cx, y: 200, width: wide ? 380 : 110, height: 10 },
    icon:  { type: "icon", icon: "mdi:star", x: cx, y: 120, size: 64 },
    image: { type: "image", source: "builtin:claude", x: cx, y: 120, width: wide ? 240 : 135, height: wide ? 240 : 135 },
    rect:  { type: "rect", x: cx, y: 120, width: wide ? 380 : 100, height: 2, color: "#444444" },
  };
  return JSON.parse(JSON.stringify(base[t]));
}
const VALUE_FIELDS = ["value", "text"];   // get the Entity/Fixed/Template source picker
const SELECTS = {                         // first option = the default, not written to the file
  align: ["center", "left", "right"],
  valign: ["middle", "top", "bottom"],
  weight: ["", "bold"],
  direction: ["", "right", "up", "left", "down"],
  style: ["area", "line", "bars"],       // graph
  eta_format: ["remaining", "time"],      // progress
  show: ["today", "now"],                  // weather
};
const MULTILINE = ["label", "unit"];      // Enter adds a line
const BOOL_FIELDS = ["show_value", "seconds", "numerals", "loop"];   // checkboxes, default = first
const HINTS = {
  size: "auto", value_size: "auto", unit_size: "auto", label_size: "auto", icon_size: "auto", text_size: "auto",
  radius: "52", thickness: "12", max: "100", start: "-90", sweep: "360",
  width: "auto size: fit in", height: "auto size: fit in",
  hours: "24", points: "12 bars / 120 line", y: "120", speed: "40",
  finish: "ISO datetime, unix timestamp or HH:MM", tz: "IANA name, e.g. Europe/Berlin",
};
const isColorField = (f) => f === "color" || f.endsWith("_color") || f === "track";
const hasTemplate = (v) => typeof v === "string" && (v.includes("{{") || v.includes("{%"));
const TUBE_W = 135, TUBE_H = 240;

const state = {
  data: null,          // parsed screens file
  yaml: "",
  mtime: null,
  errors: [],
  warnings: [],        // api/config: unknown-field warnings (the file still loads)
  isExample: false,    // the screens file is still the untouched example
  selectedScreen: null,
  selectedTube: null,
  clipboard: null,
  dirty: false,
  previewTimer: null,
  previewUrl: null,
  designerRefresh: null,   // set while a custom tube's designer is open
  designerSel: null,       // which of its items is selected
  clocks: [],              // api/clocks: [{name, host, online, rotation_enabled}]
  selectedClock: "",       // "" = all clocks (the shared rotation)
  savedJson: null,         // JSON of state.data as last loaded/saved (undo target "clean")
};
const stash = new WeakMap();   // screen -> the tubes / strip it had before its layout was switched

// undo/redo: JSON snapshots of state.data, max 100; the top of `undo` is the current
// state, so undo needs at least two entries. Changes less than 700 ms apart are
// coalesced into one step (typing a word = one step).
const HISTORY_MAX = 100;
const history = { undo: [], redo: [], lastPush: 0, lastJson: null };

const $ = (id) => document.getElementById(id);

async function api(path, opts) {
  const res = await fetch(path, opts);
  let body = null;
  // Only read JSON replies: reading a PNG here would use up the body the preview needs
  if ((res.headers.get("content-type") || "").includes("json")) {
    try { body = await res.json(); } catch (e) { /* not valid JSON */ }
  }
  return { status: res.status, body, res };
}

function setStatus(text, kind) {
  const el = $("status");
  el.textContent = text || "";
  el.className = "status" + (kind ? " " + kind : "");
  $("reloadBtn").classList.toggle("hidden", kind !== "conflict");
  // the file's warnings stay visible as a clickable "N warnings" link
  if (state.warnings && state.warnings.length) {
    const link = document.createElement("span");
    link.className = "warn-link";
    link.textContent = " " + state.warnings.length + " warning" + (state.warnings.length > 1 ? "s" : "");
    link.title = "Show the warnings";
    link.addEventListener("click", openWarnings);
    el.appendChild(link);
  }
}

function markDirty() {
  pushHistory();
  state.dirty = true;
  $("saveBtn").disabled = false;
  setStatus("unsaved changes", "dirty");
}

// ---------------------------------------------------------------- undo / redo

function snapshot() { return JSON.stringify(state.data); }

function pushHistory() {
  if (!state.data) return;
  const json = snapshot();
  const now = Date.now();
  if (history.lastJson === json) return;                 // nothing actually changed
  if (history.undo.length && now - history.lastPush < 700) {
    history.undo[history.undo.length - 1] = json;         // coalesce with the previous step
  } else {
    history.undo.push(json);
    if (history.undo.length > HISTORY_MAX) history.undo.shift();
  }
  history.redo = [];
  history.lastPush = now;
  history.lastJson = json;
  updateHistoryButtons();
}

function resetHistory() {
  history.undo = state.data ? [snapshot()] : [];
  history.redo = [];
  history.lastPush = 0;    // the first change after a load always starts a new step
  history.lastJson = history.undo.length ? history.undo[0] : null;
  state.savedJson = history.lastJson;
  updateHistoryButtons();
}

function updateHistoryButtons() {
  $("undoBtn").disabled = history.undo.length < 2;
  $("redoBtn").disabled = history.redo.length === 0;
}

function closeDesigner() {
  // same reset renderTubeEditor does when the panel is hidden: drop the designer's
  // refresh hook and its selection so nothing redraws into a detached panel
  state.designerRefresh = null;
  state.designerSel = null;
}

function applySnapshot(json) {
  const data = JSON.parse(json);
  state.data = data;
  if (!state.data.screens) state.data.screens = {};
  if (!state.data.rotation) state.data.rotation = Object.keys(state.data.screens);
  if (!state.selectedScreen || !state.data.screens[state.selectedScreen]) {
    state.selectedScreen = state.data.rotation.find((r) => r !== "clock") || Object.keys(state.data.screens)[0] || null;
  }
  if (state.selectedTube != null) {
    const s = state.data.screens[state.selectedScreen];
    if (!s || s.strip || s.canvas || !s.tubes || state.selectedTube >= s.tubes.length) state.selectedTube = null;
  }
  closeDesigner();
  renderAll();
  schedulePreview(0);
  if (json === state.savedJson) {
    state.dirty = false;
    $("saveBtn").disabled = true;
    setStatus("loaded", "");
  } else {
    state.dirty = true;
    $("saveBtn").disabled = false;
    setStatus("unsaved changes", "dirty");
  }
  history.lastJson = json;
  history.lastPush = 0;   // a change right after an undo/redo starts a new step
  updateHistoryButtons();
}

function undo() {
  if (history.undo.length < 2) return;
  history.redo.push(history.undo.pop());
  applySnapshot(history.undo[history.undo.length - 1]);
}

function redo() {
  if (!history.redo.length) return;
  const json = history.redo.pop();
  history.undo.push(json);
  applySnapshot(json);
}

$("undoBtn").addEventListener("click", undo);
$("redoBtn").addEventListener("click", redo);

document.addEventListener("keydown", (e) => {
  if (!(e.ctrlKey || e.metaKey) || e.altKey) return;
  // in a text field the browser's own undo/redo works on the text - leave it alone
  const t = e.target;
  if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable)) return;
  const key = e.key.toLowerCase();
  if (key === "z" && (e.shiftKey)) { e.preventDefault(); redo(); }
  else if (key === "z") { e.preventDefault(); undo(); }
  else if (key === "y") { e.preventDefault(); redo(); }
});

// ---------------------------------------------------------------- load

async function load() {
  const { status, body } = await api("api/config");
  if (status !== 200) { setStatus("could not load config: " + (body && body.error || status), "error"); return; }
  state.yaml = body.yaml;
  state.data = body.data || {};
  state.mtime = body.mtime;
  state.errors = body.errors || [];
  state.warnings = body.warnings || [];
  state.isExample = !!body.is_example;
  if (!state.data.screens) state.data.screens = {};
  if (!state.data.rotation) state.data.rotation = Object.keys(state.data.screens);
  if (!state.selectedScreen || !state.data.screens[state.selectedScreen]) {
    state.selectedScreen = state.data.rotation.find((r) => r !== "clock") || Object.keys(state.data.screens)[0] || null;
  }
  resetHistory();          // a (re)load starts a fresh undo history
  renderAll();
  setStatus(state.errors.length ? state.errors.length + " error(s) in file" : "loaded", state.errors.length ? "error" : "");
  schedulePreview(0);
  if (state.isExample) {
    let seen = null;
    try { seen = sessionStorage.getItem("td_setup_seen"); } catch (e) { /* private mode */ }
    if (!seen) openSetup();
  }
}

function renderAll() {
  renderClockSelect();
  renderRotation();
  renderNight();
  renderScreenList();
  renderDefaultFont();
  renderScreenSettings();
  renderTubeEditor();
}

// ---------------------------------------------------------------- rotation

// The rotation being edited: the chosen clock's override if it has one, else the
// shared one (read-only while the clock has no override - the button copies it in).
function rotationTarget() {
  const ov = overrideFor(state.selectedClock);
  if (ov && ov.rotation) return ov;
  return state.data;
}

function overrideFor(name) {
  if (!name || !state.data.clocks) return null;
  return state.data.clocks[name] || null;
}

// Where a clock-duration edit goes: the override's clock when a clock is chosen
// (created on demand), else the shared `clock:` block.
function clockTarget(create) {
  if (state.selectedClock) {
    if (!state.data.clocks) state.data.clocks = {};
    if (!state.data.clocks[state.selectedClock]) state.data.clocks[state.selectedClock] = {};
    const ov = state.data.clocks[state.selectedClock];
    if (!ov.clock) {
      if (!create) return null;
      ov.clock = {};
    }
    return ov.clock;
  }
  if (!state.data.clock && create) state.data.clock = {};
  return state.data.clock || null;
}

function renderClockSelect() {
  const row = $("clockSelectRow");
  const sel = $("clockSelect");
  if (state.clocks.length < 2) {
    row.classList.add("hidden");
    state.selectedClock = "";
    return;
  }
  row.classList.remove("hidden");
  const names = state.clocks.map((c) => c.name);
  if (!names.includes(state.selectedClock)) state.selectedClock = "";
  sel.innerHTML = "";
  const all = document.createElement("option");
  all.value = ""; all.textContent = "All clocks";
  sel.appendChild(all);
  names.forEach((n) => {
    const o = document.createElement("option");
    o.value = n; o.textContent = n;
    sel.appendChild(o);
  });
  sel.value = state.selectedClock;
}

$("clockSelect").addEventListener("change", (e) => {
  state.selectedClock = e.target.value;
  renderAll();
});

function renderRotation() {
  const ul = $("rotationList");
  ul.innerHTML = "";
  const tgt = rotationTarget();
  const rot = tgt.rotation || [];
  const own = !!(state.selectedClock && overrideFor(state.selectedClock)
                 && overrideFor(state.selectedClock).rotation);
  rot.forEach((name, i) => {
    const li = document.createElement("li");
    li.className = "rot-item" + (name === state.selectedScreen ? " selected" : "");
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = true; cb.title = "include in rotation";
    cb.disabled = !own && !!state.selectedClock;
    cb.addEventListener("change", () => { if (!cb.checked) removeFromRotation(name); markDirty(); });
    const label = document.createElement("span");
    label.textContent = name;
    label.className = "rot-name";
    label.addEventListener("click", () => { if (name !== "clock") selectScreen(name); });
    const up = mkBtn("↑", () => moveRotation(i, -1));
    const down = mkBtn("↓", () => moveRotation(i, 1));
    up.disabled = down.disabled = !own && !!state.selectedClock;
    li.append(cb, label, up, down);
    ul.appendChild(li);
  });
  // Screens left out of the rotation stay listed, unticked, so they can be put back
  Object.keys((state.data.screens) || {}).filter((n) => !rot.includes(n)).forEach((name) => {
    const li = document.createElement("li");
    li.className = "rot-item off" + (name === state.selectedScreen ? " selected" : "");
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = false; cb.title = "include in rotation";
    cb.disabled = !own && !!state.selectedClock;
    cb.addEventListener("change", () => {
      if (cb.checked && !tgt.rotation.includes(name)) {
        tgt.rotation = (tgt.rotation || []).concat([name]);
        markDirty(); renderRotation();
      }
    });
    const label = document.createElement("span");
    label.textContent = name + " (not in rotation)";
    label.className = "rot-name";
    label.addEventListener("click", () => selectScreen(name));
    li.append(cb, label);
    ul.appendChild(li);
  });
  const clockOn = rot.includes("clock");
  $("clockEnabled").checked = clockOn;
  $("clockEnabled").disabled = !own && !!state.selectedClock;
  // the duration shown is the override's when there is one, else the shared one
  const ov = overrideFor(state.selectedClock);
  const clock = (ov && ov.clock) || state.data.clock || {};
  $("clockDuration").value = clock.duration != null ? clock.duration : "";
  $("clockDuration").disabled = !own && !!state.selectedClock;
  renderClockOverrideRow(own);
}

// With 2+ clocks and one of them chosen: offer to give it its own rotation, or to
// drop the override and go back to the shared one.
function renderClockOverrideRow(own) {
  const row = $("clockOverrideRow");
  const name = state.selectedClock;
  if (state.clocks.length < 2 || !name) { row.classList.add("hidden"); return; }
  row.classList.remove("hidden");
  $("clockOverrideHint").textContent = own
    ? name + " has its own rotation."
    : "The shared rotation is shown greyed.";
  const btn = $("clockOverrideBtn");
  if (own) {
    btn.textContent = "Use the shared rotation";
    btn.onclick = () => {
      const ov = overrideFor(name);
      delete ov.rotation;
      if (ov.clock && !Object.keys(ov.clock).length) delete ov.clock;
      if (!Object.keys(ov).length) delete state.data.clocks[name];
      if (state.data.clocks && !Object.keys(state.data.clocks).length) delete state.data.clocks;
      markDirty(); renderAll();
    };
  } else {
    btn.textContent = "Give " + name + " its own rotation";
    btn.onclick = () => {
      if (!state.data.clocks) state.data.clocks = {};
      if (!state.data.clocks[name]) state.data.clocks[name] = {};
      state.data.clocks[name].rotation = (state.data.rotation || []).slice();
      markDirty(); renderAll();
    };
  }
  // while the clock has no override of its own, the shared list is only shown, greyed
  $("rotationList").classList.toggle("greyed", !own);
}

function mkBtn(text, fn, title) {
  const b = document.createElement("button");
  b.className = "btn small"; b.textContent = text;
  if (title) b.title = title;
  b.addEventListener("click", fn);
  return b;
}

function moveRotation(i, dir) {
  const rot = rotationTarget().rotation;
  const j = i + dir;
  if (j < 0 || j >= rot.length) return;
  [rot[i], rot[j]] = [rot[j], rot[i]];
  markDirty(); renderRotation();
}

function removeFromRotation(name) {
  const tgt = rotationTarget();
  tgt.rotation = tgt.rotation.filter((r) => r !== name);
  renderRotation();
}

$("clockEnabled").addEventListener("change", (e) => {
  const tgt = rotationTarget();
  const rot = tgt.rotation;
  if (e.target.checked && !rot.includes("clock")) rot.push("clock");
  if (!e.target.checked) tgt.rotation = rot.filter((r) => r !== "clock");
  markDirty(); renderRotation();
});
$("clockDuration").addEventListener("change", (e) => {
  const v = parseInt(e.target.value, 10);
  const clock = clockTarget(true);
  if (v > 0) clock.duration = v;
  else delete clock.duration;
  // an emptied clock entry is removed again (and the clocks: block with it)
  const ov = overrideFor(state.selectedClock);
  if (ov && ov.clock && !Object.keys(ov.clock).length) {
    delete ov.clock;
    if (!Object.keys(ov).length) delete state.data.clocks[state.selectedClock];
    if (state.data.clocks && !Object.keys(state.data.clocks).length) delete state.data.clocks;
  }
  if (!state.selectedClock && state.data.clock && !Object.keys(state.data.clock).length) delete state.data.clock;
  markDirty();
});

// ---------------------------------------------------------------- night mode

const NIGHT_DEFAULTS = { brightness_day: 255, brightness_night: 50, bright_above: 3, dim_below: -6, dim_screens: 0.6 };

function renderNight() {
  const n = state.data.night;
  $("nightEnabled").checked = !!n;
  $("nightFields").classList.toggle("hidden", !n);
  if (!n) return;
  $("nightDay").value = n.brightness_day != null ? n.brightness_day : NIGHT_DEFAULTS.brightness_day;
  $("nightNight").value = n.brightness_night != null ? n.brightness_night : NIGHT_DEFAULTS.brightness_night;
  $("nightAbove").value = n.bright_above != null ? n.bright_above : NIGHT_DEFAULTS.bright_above;
  $("nightBelow").value = n.dim_below != null ? n.dim_below : NIGHT_DEFAULTS.dim_below;
  const dim = n.dim_screens != null ? n.dim_screens : NIGHT_DEFAULTS.dim_screens;
  $("nightDim").value = Math.round(dim * 100);
  $("nightDayVal").textContent = $("nightDay").value;
  $("nightNightVal").textContent = $("nightNight").value;
  $("nightDimVal").textContent = $("nightDim").value + "%";
  const q = n.quiet;
  $("nightQuiet").checked = !!q;
  $("quietFields").classList.toggle("hidden", !q);
  if (q) {
    $("quietFrom").value = q.from || "23:00";
    $("quietTo").value = q.to || "07:00";
    $("quietMode").value = q.mode === "blank" ? "blank" : "clock";
  }
}

$("nightEnabled").addEventListener("change", (e) => {
  if (e.target.checked) state.data.night = Object.assign({}, NIGHT_DEFAULTS);
  else delete state.data.night;
  markDirty(); renderNight();
});

function nightNum(id, key) {
  $(id).addEventListener("input", (e) => {
    const n = state.data.night; if (!n) return;
    const v = parseFloat(e.target.value);
    if (isFinite(v)) n[key] = v; else delete n[key];
    if (id === "nightDay" || id === "nightNight") $(id + "Val").textContent = e.target.value;
    if (id === "nightDim") $("nightDimVal").textContent = e.target.value + "%";
    markDirty();
  });
  $(id).addEventListener("change", (e) => {
    const n = state.data.night; if (!n) return;
    const v = parseFloat(e.target.value);
    if (isFinite(v)) n[key] = v; else delete n[key];
    markDirty(); renderNight();
  });
}
nightNum("nightDay", "brightness_day");
nightNum("nightNight", "brightness_night");
nightNum("nightAbove", "bright_above");
nightNum("nightBelow", "dim_below");
$("nightDim").addEventListener("input", (e) => {
  const n = state.data.night; if (!n) return;
  n.dim_screens = Math.round(parseInt(e.target.value, 10) || 100) / 100;
  $("nightDimVal").textContent = e.target.value + "%";
  markDirty();
});
$("nightDim").addEventListener("change", () => { markDirty(); renderNight(); });

$("nightQuiet").addEventListener("change", (e) => {
  const n = state.data.night; if (!n) return;
  if (e.target.checked) n.quiet = { from: "23:00", to: "07:00", mode: "clock" };
  else delete n.quiet;
  markDirty(); renderNight();
});
$("quietFrom").addEventListener("change", (e) => {
  const n = state.data.night; if (!n || !n.quiet) return;
  if (e.target.value) n.quiet.from = e.target.value; else delete n.quiet.from;
  markDirty();
});
$("quietTo").addEventListener("change", (e) => {
  const n = state.data.night; if (!n || !n.quiet) return;
  if (e.target.value) n.quiet.to = e.target.value; else delete n.quiet.to;
  markDirty();
});
$("quietMode").addEventListener("change", (e) => {
  const n = state.data.night; if (!n || !n.quiet) return;
  n.quiet.mode = e.target.value;
  markDirty();
});

// ---------------------------------------------------------------- screens list

function renderScreenList() {
  const ul = $("screenList");
  ul.innerHTML = "";
  Object.keys(state.data.screens).forEach((name) => {
    const li = document.createElement("li");
    li.className = "screen-item" + (name === state.selectedScreen ? " selected" : "");
    li.textContent = name;
    li.addEventListener("click", () => selectScreen(name));
    ul.appendChild(li);
  });
}

function selectScreen(name) {
  state.selectedScreen = name;
  state.selectedTube = null;
  renderAll();
  schedulePreview(0);
}

// every rotation list in the file (the shared one plus each clock's override)
function allRotations() {
  const out = [state.data.rotation];
  Object.values(state.data.clocks || {}).forEach((ov) => { if (ov.rotation) out.push(ov.rotation); });
  return out;
}

$("addScreen").addEventListener("click", openGallery);

function addBlankScreen() {
  let name = "screen" + (Object.keys(state.data.screens).length + 1);
  while (state.data.screens[name]) name = "s" + Math.random().toString(36).slice(2, 7);
  state.data.screens[name] = { background: "#000000", tubes: Array.from({ length: 6 }, () => ({ type: "blank" })) };
  if (!state.data.rotation.includes(name)) state.data.rotation.push(name);
  selectScreen(name);
  markDirty();
}

$("renameScreen").addEventListener("click", () => {
  const old = state.selectedScreen;
  if (!old) return;
  const name = prompt("Rename screen", old);
  if (!name || name === old || state.data.screens[name]) return;
  state.data.screens[name] = state.data.screens[old];
  delete state.data.screens[old];
  allRotations().forEach((rot) => {
    const i = rot.indexOf(old);
    if (i >= 0) rot[i] = name;
  });
  state.selectedScreen = name;
  markDirty(); renderAll();
});

$("deleteScreen").addEventListener("click", () => {
  const name = state.selectedScreen;
  if (!name || !confirm("Delete screen '" + name + "'?")) return;
  delete state.data.screens[name];
  allRotations().forEach((rot) => {
    const i = rot.indexOf(name);
    if (i >= 0) rot.splice(i, 1);
  });
  state.selectedScreen = Object.keys(state.data.screens)[0] || null;
  state.selectedTube = null;
  markDirty(); renderAll(); schedulePreview(0);
});

// ---------------------------------------------------------------- gallery (Add a screen)

const gal = { templates: [], template: null, previewUrl: null };

function showErrors(ul, errs) {
  ul.innerHTML = "";
  (errs || []).forEach((e) => {
    const li = document.createElement("li"); li.textContent = e; ul.appendChild(li);
  });
}

async function previewBlob(path, body) {
  const { status, res } = await api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (status !== 200) return null;
  try { return await res.blob(); } catch (e) { return null; }
}

function setPreviewImg(img, blob) {
  if (gal.previewUrl) { URL.revokeObjectURL(gal.previewUrl); gal.previewUrl = null; }
  if (blob) {
    gal.previewUrl = URL.createObjectURL(blob);
    img.src = gal.previewUrl;
    img.classList.remove("hidden");
  } else {
    img.removeAttribute("src");
    img.classList.add("hidden");
  }
}

async function openGallery() {
  $("galleryModal").classList.remove("hidden");
  $("galleryGridView").classList.remove("hidden");
  $("galleryFormView").classList.add("hidden");
  showErrors($("galleryErrors"), []);
  $("galleryGrid").innerHTML = "";
  const { status, body } = await api("api/gallery");
  if (status !== 200 || !body || !Array.isArray(body.templates)) {
    showErrors($("galleryErrors"), ["could not load the gallery: " + ((body && body.error) || status)]);
    return;
  }
  gal.templates = body.templates;
  // the blank screen is the first card, like the old "Add" flow
  const blank = document.createElement("button");
  blank.className = "gallery-card";
  blank.innerHTML = '<span class="g-name">Blank screen</span>' +
    '<span class="g-desc">Six empty tubes, you fill them in yourself.</span>';
  blank.addEventListener("click", () => { closeGallery(); addBlankScreen(); });
  $("galleryGrid").appendChild(blank);
  body.templates.forEach((t) => {
    const card = document.createElement("button");
    card.className = "gallery-card";
    card.dataset.id = t.id;
    const img = document.createElement("img");
    img.alt = t.name + " preview";
    card.appendChild(img);
    const nm = document.createElement("span"); nm.className = "g-name"; nm.textContent = t.name;
    const ds = document.createElement("span"); ds.className = "g-desc"; ds.textContent = t.description;
    card.append(nm, ds);
    card.addEventListener("click", () => openGalleryForm(t));
    $("galleryGrid").appendChild(card);
    // lazy preview: only fetched once the card is on screen
    const io = new IntersectionObserver((entries) => {
      if (!entries.some((en) => en.isIntersecting)) return;
      io.disconnect();
      previewBlob("api/gallery/preview", { id: t.id, values: {} }).then((b) => { if (b) img.src = URL.createObjectURL(b); });
    });
    io.observe(card);
  });
}

function closeGallery() { $("galleryModal").classList.add("hidden"); }

// One entity input of a gallery template / wizard suggestion: a text box (the global
// autocomplete works in it) plus a list of HA entities of the wanted domain/device_class.
function entityInputRow(opts) {
  const row = document.createElement("div");
  row.className = "field";
  const lab = document.createElement("span");
  lab.className = "field-label";
  lab.textContent = opts.label;
  const input = document.createElement("input");
  input.type = "text";
  input.dataset.entity = "1";
  input.placeholder = opts.domain + ".…";
  input.value = opts.value || "";
  const list = document.createElement("div");
  list.className = "entity-list";
  let timer = null;
  async function search() {
    const q = input.value.trim();
    const { status, body } = await api("api/entities?q=" + encodeURIComponent(q));
    list.innerHTML = "";
    if (status !== 200 || !Array.isArray(body)) return;
    body.filter((e) => (!opts.domain || e.domain === opts.domain) &&
                       (!opts.deviceClass || e.device_class === opts.deviceClass))
        .slice(0, 8)
        .forEach((e) => {
          const b = document.createElement("button");
          b.type = "button"; b.className = "entity-opt";
          b.textContent = e.entity_id + (e.name && e.name !== e.entity_id ? " (" + e.name + ")" : "") +
            (e.state != null ? " — " + e.state + (e.unit || "") : "");
          b.addEventListener("click", () => {
            input.value = e.entity_id;
            if (opts.onChange) opts.onChange(e.entity_id);
            input.dispatchEvent(new Event("input", { bubbles: true }));
          });
          list.appendChild(b);
        });
  }
  input.addEventListener("input", () => {
    if (opts.onChange) opts.onChange(input.value.trim());
    clearTimeout(timer);
    timer = setTimeout(search, 250);
  });
  row.append(lab, input, list);
  search();
  return { row, input };
}

function galleryValues() {
  const values = {};
  if (!gal.template) return values;
  gal.template.inputs.forEach((inp) => {
    const el = $("galIn_" + inp.key);
    if (el) values[inp.key] = el.value.trim();
  });
  return values;
}

async function refreshGalleryPreview() {
  if (!gal.template) return;
  const blob = await previewBlob("api/gallery/preview", { id: gal.template.id, values: galleryValues() });
  setPreviewImg($("galleryPreview"), blob);
}

function openGalleryForm(t) {
  gal.template = t;
  $("galleryGridView").classList.add("hidden");
  $("galleryFormView").classList.remove("hidden");
  $("galleryFormDesc").textContent = t.description;
  $("galleryName").value = t.name;
  showErrors($("galleryFormErrors"), []);
  const box = $("galleryInputs");
  box.innerHTML = "";
  t.inputs.forEach((inp) => {
    const { row, input } = entityInputRow({
      label: inp.label,
      domain: inp.domain,
      deviceClass: inp.device_class,
      onChange: () => refreshGalleryPreview(),
    });
    input.id = "galIn_" + inp.key;
    box.appendChild(row);
  });
  refreshGalleryPreview();
}

$("galleryClose").addEventListener("click", closeGallery);
$("galleryModal").addEventListener("click", (e) => { if (e.target === $("galleryModal")) closeGallery(); });
$("galleryBack").addEventListener("click", () => {
  $("galleryFormView").classList.add("hidden");
  $("galleryGridView").classList.remove("hidden");
  gal.template = null;
});
$("galleryName").addEventListener("input", () => refreshGalleryPreview());
$("galleryAdd").addEventListener("click", async () => {
  if (!gal.template) return;
  showErrors($("galleryFormErrors"), []);
  const { status, body } = await api("api/gallery/build", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: gal.template.id, values: galleryValues(), name: $("galleryName").value }),
  });
  if (status !== 200 || !body || !body.screen) {
    showErrors($("galleryFormErrors"), [(body && (body.error || (body.errors || []).join("; "))) || "build failed: " + status]);
    return;
  }
  state.data.screens[body.name] = body.screen;
  if (!state.data.rotation.includes(body.name)) state.data.rotation.push(body.name);
  closeGallery();
  selectScreen(body.name);
  markDirty();
});

// ---------------------------------------------------------------- import / export

async function openExport() {
  const name = state.selectedScreen;
  const s = curScreen();
  if (!name || !s) return;
  $("exportModal").classList.remove("hidden");
  $("exportTitle").textContent = "Export screen: " + name;
  $("exportYaml").textContent = "loading…";
  showErrors($("exportErrors"), []);
  const { status, body } = await api("api/export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, screen: s }),
  });
  if (status !== 200 || !body || typeof body.yaml !== "string") {
    $("exportYaml").textContent = "";
    showErrors($("exportErrors"), [(body && body.error) || "export failed: " + status]);
    return;
  }
  $("exportYaml").textContent = body.yaml;
  $("exportYaml").dataset.name = name;
}

function closeExport() { $("exportModal").classList.add("hidden"); }

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (e) { /* fall through to the textarea fallback */ }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed"; ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
  ta.remove();
  return ok;
}

$("exportScreen").addEventListener("click", openExport);
$("exportClose").addEventListener("click", closeExport);
$("exportDone").addEventListener("click", closeExport);
$("exportModal").addEventListener("click", (e) => { if (e.target === $("exportModal")) closeExport(); });
$("exportCopy").addEventListener("click", async () => {
  const ok = await copyText($("exportYaml").textContent);
  setStatus(ok ? "copied to clipboard" : "copy failed - select the text manually", ok ? "ok" : "error");
});
$("exportDownload").addEventListener("click", () => {
  const name = $("exportYaml").dataset.name || "screen";
  const blob = new Blob([$("exportYaml").textContent], { type: "text/yaml" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name.replace(/[^a-z0-9_-]+/gi, "_") + ".yaml";
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
});

function openImport() {
  $("importModal").classList.remove("hidden");
  $("importText").value = "";
  showErrors($("importErrors"), []);
}
function closeImport() { $("importModal").classList.add("hidden"); }

$("importScreens").addEventListener("click", openImport);
$("importClose").addEventListener("click", closeImport);
$("importCancel").addEventListener("click", closeImport);
$("importModal").addEventListener("click", (e) => { if (e.target === $("importModal")) closeImport(); });
$("importDo").addEventListener("click", async () => {
  showErrors($("importErrors"), []);
  const { status, body } = await api("api/import", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ yaml: $("importText").value }),
  });
  if (status !== 200 || !body) {
    showErrors($("importErrors"), [(body && (body.error || (body.errors || []).join("; "))) || "import failed: " + status]);
    return;
  }
  if (body.errors && body.errors.length) {
    showErrors($("importErrors"), body.errors);
    return;
  }
  const names = Object.keys(body.screens || {});
  if (!names.length) { showErrors($("importErrors"), ["no valid screens in that YAML"]); return; }
  names.forEach((n) => { state.data.screens[n] = body.screens[n]; });
  if ($("importRotation").checked) {
    names.forEach((n) => { if (!state.data.rotation.includes(n)) state.data.rotation.push(n); });
  }
  closeImport();
  selectScreen(names[0]);
  markDirty();
});

// ---------------------------------------------------------------- setup wizard

const setup = { step: 1, suggestions: [], built: [] };

function setupShow(step) {
  setup.step = step;
  [1, 2, 3, 4].forEach((n) => $("setupStep" + n).classList.toggle("hidden", n !== step));
}

async function openSetup() {
  try { sessionStorage.setItem("td_setup_seen", "1"); } catch (e) { /* private mode */ }
  $("setupModal").classList.remove("hidden");
  setupShow(1);
  await drawSetupClocks();
  if (!gal.templates.length) {
    const { status, body } = await api("api/gallery");
    if (status === 200 && body && Array.isArray(body.templates)) gal.templates = body.templates;
  }
}
function closeSetup() { $("setupModal").classList.add("hidden"); }

async function drawSetupClocks() {
  const ul = $("setupClocks");
  ul.innerHTML = "";
  showErrors($("setupStep1Errors"), []);
  const { status, body } = await api("api/clocks");
  const clocks = (status === 200 && body && Array.isArray(body.clocks)) ? body.clocks : [];
  $("setupNoClocks").classList.toggle("hidden", clocks.length > 0);
  clocks.forEach((c) => {
    const li = document.createElement("li");
    li.textContent = c.name + "  —  " + (c.host || "no host") + "  —  " + (c.online ? "online" : "offline") +
      (c.rotation_enabled ? "  —  cycling" : "");
    ul.appendChild(li);
  });
}

async function drawSetupSuggestions() {
  const box = $("setupSuggestions");
  box.innerHTML = "";
  showErrors($("setupStep2Errors"), []);
  const { status, body } = await api("api/suggest");
  if (status !== 200 || !body || !Array.isArray(body.suggestions)) {
    showErrors($("setupStep2Errors"), ["could not load suggestions: " + ((body && body.error) || status)]);
    return;
  }
  setup.suggestions = body.suggestions;
  body.suggestions.forEach((s, i) => {
    const t = (gal.templates.find((x) => x.id === s.id)) || { id: s.id, name: s.name, inputs: [] };
    const row = document.createElement("div");
    row.className = "suggest-row";
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = true; cb.dataset.idx = i;
    const nm = document.createElement("strong");
    nm.textContent = s.name || t.name;
    const why = document.createElement("span");
    why.className = "why";
    why.textContent = s.why || "";
    row.append(cb, nm, why);
    // one entity input per template input, pre-filled from the suggestion
    t.inputs.forEach((inp) => {
      const { row: erow, input } = entityInputRow({
        label: inp.label,
        domain: inp.domain,
        deviceClass: inp.device_class,
        value: (s.values || {})[inp.key] || "",
      });
      input.dataset.sug = i;
      input.dataset.key = inp.key;
      erow.classList.add("suggest-entity");
      row.appendChild(erow);
    });
    box.appendChild(row);
  });
}

function setupSelected() {
  const out = [];
  document.querySelectorAll("#setupSuggestions input[type=checkbox]").forEach((cb) => {
    if (!cb.checked) return;
    const s = setup.suggestions[Number(cb.dataset.idx)];
    if (!s) return;
    const values = {};
    Object.assign(values, s.values || {});
    document.querySelectorAll('#setupSuggestions input[data-sug="' + cb.dataset.idx + '"]').forEach((inp) => {
      values[inp.dataset.key] = inp.value.trim();
    });
    out.push({ id: s.id, name: s.name, values });
  });
  return out;
}

function drawSetupRotation(names) {
  const ul = $("setupRotation");
  ul.innerHTML = "";
  names.forEach((name, i) => {
    const li = document.createElement("li");
    li.className = "rot-item";
    const span = document.createElement("span");
    span.className = "rot-name";
    span.textContent = (i + 1) + ". " + name;
    li.appendChild(span);
    li.appendChild(mkBtn("↑", () => {
      if (i > 0) { [names[i - 1], names[i]] = [names[i], names[i - 1]]; drawSetupRotation(names); }
    }));
    li.appendChild(mkBtn("↓", () => {
      if (i < names.length - 1) { [names[i + 1], names[i]] = [names[i], names[i + 1]]; drawSetupRotation(names); }
    }));
    ul.appendChild(li);
  });
  setup.rotationNames = names;
}

$("setupBtn").addEventListener("click", openSetup);
$("setupClose").addEventListener("click", closeSetup);
$("setupModal").addEventListener("click", (e) => { if (e.target === $("setupModal")) closeSetup(); });
$("setupSkipClock").addEventListener("click", () => setupShow(2));
$("setupStep1Next").addEventListener("click", async () => { await drawSetupSuggestions(); setupShow(2); });
$("setupStep2Back").addEventListener("click", () => setupShow(1));
$("setupStep2Next").addEventListener("click", () => {
  const sel = setupSelected();
  if (!sel.length) { showErrors($("setupStep2Errors"), ["tick at least one screen"]); return; }
  drawSetupRotation(sel.map((s) => s.name));
  setupShow(3);
});
$("setupStep3Back").addEventListener("click", () => setupShow(2));
$("setupStep3Next").addEventListener("click", () => {
  $("setupReplaceNote").classList.toggle("hidden", state.isExample);
  const ul = $("setupSummary");
  ul.innerHTML = "";
  setupSelected().forEach((s) => {
    const li = document.createElement("li");
    li.textContent = s.name + "  (" + Object.values(s.values).filter(Boolean).join(", ") + ")";
    ul.appendChild(li);
  });
  setupShow(4);
});
$("setupStep4Back").addEventListener("click", () => setupShow(3));
$("setupFinish").addEventListener("click", async () => {
  showErrors($("setupStep4Errors"), []);
  const sel = setupSelected();
  const built = [];
  for (const s of sel) {
    const { status, body } = await api("api/gallery/build", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: s.id, values: s.values, name: s.name }),
    });
    if (status !== 200 || !body || !body.screen) {
      showErrors($("setupStep4Errors"), [(body && body.error) || (s.name + ": build failed")]);
      return;
    }
    built.push({ name: body.name, screen: body.screen });
  }
  if (!state.isExample && !confirm("Replace the current screens with these " + built.length + "?")) return;
  const duration = parseInt($("setupDuration").value, 10);
  state.data.screens = {};
  built.forEach((b) => {
    state.data.screens[b.name] = b.screen;
    if (duration > 0) b.screen.duration = duration;
  });
  state.data.rotation = built.map((b) => b.name);
  closeSetup();
  selectScreen(built[0].name);
  markDirty();
  // cycle automatically on/off, like the topbar toggle
  const { status: rstatus } = await api("api/rotation", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: $("setupCycle").checked }),
  });
  if (rstatus !== 200) showErrors($("setupStep4Errors"), ["screens added, but cycling could not be set"]);
  // save through the normal API
  const { status, body } = await api("api/save", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ data: state.data, mtime: state.mtime }),
  });
  if (status === 200) {
    state.mtime = body.mtime;
    state.dirty = false;
    $("saveBtn").disabled = true;
    state.savedJson = snapshot();
    history.lastJson = state.savedJson;
    updateHistoryButtons();
    const cfg = await api("api/config");
    if (cfg.status === 200) {
      state.yaml = cfg.body.yaml; state.errors = cfg.body.errors || [];
      state.warnings = cfg.body.warnings || []; state.isExample = !!cfg.body.is_example;
    }
    setStatus("setup saved " + built.length + " screen(s)", "ok");
  } else if (status === 409) {
    setStatus("changed on disk — reload to keep your edits safe", "conflict");
  } else {
    setStatus("save failed: " + ((body && (body.errors || [body.error]).join("; ")) || status), "error");
  }
});

// ---------------------------------------------------------------- when builder

const when = { spec: null, timer: null };

function whenReadSpec() {
  const conditions = [];
  document.querySelectorAll("#whenConditions .when-cond").forEach((row) => {
    const entity = row.querySelector(".wc-entity").value.trim();
    const op = row.querySelector(".wc-op").value;
    const value = row.querySelector(".wc-value").value.trim();
    const value2 = row.querySelector(".wc-value2");
    if (!entity) return;
    const c = { entity, op, value };
    if (op === "between" && value2) c.value2 = value2.value.trim();
    conditions.push(c);
  });
  const time = $("whenTimeEnabled").checked && $("whenTimeFrom").value && $("whenTimeTo").value
    ? { from: $("whenTimeFrom").value, to: $("whenTimeTo").value } : null;
  return { combine: $("whenCombine").value, conditions, time };
}

function whenDrawSpec(spec) {
  $("whenCombine").value = spec.combine === "any" ? "any" : "all";
  const box = $("whenConditions");
  box.innerHTML = "";
  (spec.conditions || []).forEach((c) => box.appendChild(whenCondRow(c)));
  if (!spec.conditions || !spec.conditions.length) box.appendChild(whenCondRow(null));
  if (spec.time) {
    $("whenTimeEnabled").checked = true;
    $("whenTimeFrom").value = spec.time.from;
    $("whenTimeTo").value = spec.time.to;
  } else {
    $("whenTimeEnabled").checked = false;
  }
}

function whenCondRow(c) {
  const row = document.createElement("div");
  row.className = "when-cond";
  const ent = document.createElement("input");
  ent.type = "text"; ent.className = "wc-entity"; ent.dataset.entity = "1";
  ent.placeholder = "entity…"; ent.value = (c && c.entity) || "";
  const op = document.createElement("select");
  op.className = "wc-op";
  [["is", "is"], ["is not", "is not"], ["above", "above"], ["below", "below"], ["between", "between"]]
    .forEach(([v, t]) => {
      const o = document.createElement("option"); o.value = v; o.textContent = t; op.appendChild(o);
    });
  op.value = (c && c.op) || "is";
  const val = document.createElement("input");
  val.type = "text"; val.className = "wc-value"; val.placeholder = "value";
  val.value = (c && c.value != null) ? String(c.value) : "";
  const val2 = document.createElement("input");
  val2.type = "text"; val2.className = "wc-value2"; val2.placeholder = "and";
  val2.value = (c && c.value2 != null) ? String(c.value2) : "";
  const del = mkBtn("✕", () => { row.remove(); whenUpdate(); }, "remove this condition");
  function sync() {
    const numeric = op.value === "above" || op.value === "below" || op.value === "between";
    val.type = numeric ? "number" : "text";
    val2.style.display = op.value === "between" ? "" : "none";
    whenUpdate();
  }
  op.addEventListener("change", sync);
  [ent, val, val2].forEach((el) => el.addEventListener("input", whenUpdate));
  sync();
  row.append(ent, op, val, val2, del);
  return row;
}

async function whenUpdate() {
  showErrors($("whenErrors"), []);
  let template = "";
  try {
    template = WhenBuilder.whenToTemplate(whenReadSpec());
  } catch (err) {
    $("whenPreview").textContent = "";
    $("whenResult").textContent = "";
    showErrors($("whenErrors"), [String(err.message || err)]);
    return;
  }
  $("whenPreview").textContent = template;
  $("whenResult").textContent = "checking…";
  const { status, body } = await api("api/template", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ template }),
  });
  if (status === 200 && body && "result" in body) {
    const r = String(body.result).trim().toLowerCase();
    $("whenResult").textContent = (r === "true" ? "true now" : r === "false" ? "false now" : "renders: " + body.result);
  } else {
    $("whenResult").textContent = (body && body.error) ? "could not render: " + body.error : "no HA client";
  }
}

function openWhen() {
  const s = curScreen();
  if (!s) return;
  $("whenModal").classList.remove("hidden");
  const current = s.when || "";
  const spec = current ? WhenBuilder.templateToWhen(current) : null;
  $("whenHandwritten").classList.toggle("hidden", !!spec);
  whenDrawSpec(spec || { combine: "all", conditions: [], time: null });
  whenUpdate();
}
function closeWhen() { $("whenModal").classList.add("hidden"); }

$("whenBuild").addEventListener("click", openWhen);
$("whenClose").addEventListener("click", closeWhen);
$("whenCancel").addEventListener("click", closeWhen);
$("whenModal").addEventListener("click", (e) => { if (e.target === $("whenModal")) closeWhen(); });
$("whenAddCond").addEventListener("click", () => {
  $("whenConditions").appendChild(whenCondRow(null));
  whenUpdate();
});
$("whenCombine").addEventListener("change", whenUpdate);
$("whenTimeEnabled").addEventListener("change", whenUpdate);
$("whenTimeFrom").addEventListener("change", whenUpdate);
$("whenTimeTo").addEventListener("change", whenUpdate);
$("whenApply").addEventListener("click", () => {
  const s = curScreen();
  if (!s) return;
  let template;
  try {
    template = WhenBuilder.whenToTemplate(whenReadSpec());
  } catch (err) {
    showErrors($("whenErrors"), [String(err.message || err)]);
    return;
  }
  if (template === "{{ true }}") delete s.when; else s.when = template;
  $("scrWhen").value = s.when || "";
  closeWhen();
  markDirty(); schedulePreview();
});

// ---------------------------------------------------------------- warnings modal

function openWarnings() {
  const ul = $("warningsList");
  ul.innerHTML = "";
  (state.warnings || []).forEach((w) => {
    const li = document.createElement("li");
    li.textContent = w;
    const m = w.match(/screen '([^']+)'/);
    if (m && state.data.screens[m[1]]) {
      li.title = "jump to screen '" + m[1] + "'";
      li.addEventListener("click", () => {
        closeWarnings();
        selectScreen(m[1]);
      });
    }
    ul.appendChild(li);
  });
  $("warningsModal").classList.remove("hidden");
}
function closeWarnings() { $("warningsModal").classList.add("hidden"); }
$("warningsClose").addEventListener("click", closeWarnings);
$("warningsModal").addEventListener("click", (e) => { if (e.target === $("warningsModal")) closeWarnings(); });
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  [["galleryModal", closeGallery], ["importModal", closeImport], ["exportModal", closeExport],
   ["setupModal", closeSetup], ["whenModal", closeWhen], ["warningsModal", closeWarnings]]
    .forEach(([id, fn]) => { if (!$(id).classList.contains("hidden")) fn(); });
});

// ---------------------------------------------------------------- screen settings

// The top-level `font:` default, shown in the Screens card. Options come from api/fonts
// (bundled families + uploaded files); the preview image next to it is refreshed on change.
async function renderDefaultFont() {
  const sel = $("defaultFont");
  const { bundled, files } = await fontList();
  sel.innerHTML = "";
  const def = document.createElement("option");
  def.value = "default"; def.textContent = "Default";
  sel.appendChild(def);
  (bundled || []).forEach((b) => {
    if (b.id === "default") return;
    const o = document.createElement("option");
    o.value = b.id; o.textContent = b.name;
    sel.appendChild(o);
  });
  if (files && files.length) {
    const grp = document.createElement("optgroup");
    grp.label = "Your fonts";
    files.forEach((fl) => {
      const o = document.createElement("option");
      o.value = "file:" + fl.name; o.textContent = fl.name;
      grp.appendChild(o);
    });
    sel.appendChild(grp);
  }
  const cur = state.data.font != null ? String(state.data.font) : "default";
  if ([...sel.options].some((o) => o.value === cur)) sel.value = cur;
}

$("defaultFont").addEventListener("change", (e) => {
  if (e.target.value === "default") delete state.data.font; else state.data.font = e.target.value;
  markDirty(); schedulePreview(0);
});

function renderScreenSettings() {
  const s = state.data.screens[state.selectedScreen];
  $("screenTitle").textContent = state.selectedScreen ? "Screen: " + state.selectedScreen : "No screen selected";
  if (!s) return;
  $("scrDuration").value = s.duration != null ? s.duration : "";
  $("scrRefresh").value = s.refresh != null ? s.refresh : "";
  $("scrBackground").value = /^#[0-9a-fA-F]{6}$/.test(s.background || "") ? s.background : "#000000";
  $("scrWhen").value = s.when || "";
  $("scrLayout").value = s.strip ? "strip" : s.canvas ? "canvas" : "tubes";
  $("scrTransition").value = s.transition != null ? s.transition : "";
}

function bindScreenSettings() {
  $("scrDuration").addEventListener("change", (e) => {
    const s = curScreen(); if (!s) return;
    const v = parseInt(e.target.value, 10);
    if (v > 0) s.duration = v; else delete s.duration;
    markDirty(); schedulePreview();
  });
  $("scrRefresh").addEventListener("change", (e) => {
    const s = curScreen(); if (!s) return;
    const v = parseInt(e.target.value, 10);
    if (v > 0) s.refresh = v; else delete s.refresh;
    markDirty(); schedulePreview();
  });
  $("scrBackground").addEventListener("change", (e) => {
    const s = curScreen(); if (!s) return;
    s.background = e.target.value;
    markDirty(); schedulePreview();
  });
  $("scrWhen").addEventListener("change", (e) => {
    const s = curScreen(); if (!s) return;
    if (e.target.value.trim()) s.when = e.target.value; else delete s.when;
    markDirty(); schedulePreview();
  });
  $("scrTransition").addEventListener("change", (e) => {
    const s = curScreen(); if (!s) return;
    if (e.target.value) s.transition = e.target.value; else delete s.transition;  // "" = default
    markDirty(); schedulePreview();
  });
}

function curScreen() { return state.data.screens[state.selectedScreen]; }

$("scrLayout").addEventListener("change", (e) => {
  const s = curScreen(); if (!s) return;
  const keep = stash.get(s) || {};
  if (e.target.value === "strip" && !s.strip) {
    keep.tubes = s.tubes; keep.canvas = s.canvas; delete s.tubes; delete s.canvas;
    s.strip = keep.strip || { source: "builtin:claude", fit: "contain" };
  } else if (e.target.value === "tubes" && (s.strip || s.canvas)) {
    if (s.strip) keep.strip = s.strip; else keep.canvas = s.canvas;
    delete s.strip; delete s.canvas;
    s.tubes = keep.tubes || Array.from({ length: 6 }, () => ({ type: "blank" }));
  } else if (e.target.value === "canvas" && !s.canvas) {
    keep.tubes = s.tubes; keep.strip = s.strip; delete s.tubes; delete s.strip;
    s.canvas = keep.canvas || canvasFromTubes(s, keep.tubes);
  }
  stash.set(s, keep);
  state.selectedTube = null;
  markDirty(); renderTubeEditor(); schedulePreview(0);
});

// tubes -> canvas: each tube's contents become items offset by its tube index * 135.
// Custom tubes keep their items as they are; the simple widget types are converted with
// toCustom (the same conversion the type select uses). Blank tubes add nothing.
function canvasFromTubes(s, tubes) {
  const items = [];
  (tubes || []).forEach((w, i) => {
    if (!w || w.type === "blank" || !w.type) return;
    const off = i * TUBE_W;
    const src = w.type === "custom" ? w : toCustom(w);
    (src.items || []).forEach((it) => {
      const copy = JSON.parse(JSON.stringify(it));
      if (typeof copy.x === "number") copy.x += off;
      else if (typeof copy.x === "string" && !hasTemplate(copy.x) && isFinite(Number(copy.x))) copy.x = Number(copy.x) + off;
      items.push(copy);
    });
  });
  if (!items.length) items.push(itemNew("text", 6 * TUBE_W));
  return { background: s.background, items };
}

// ---------------------------------------------------------------- preview

function schedulePreview(ms) {
  clearTimeout(state.previewTimer);
  state.previewTimer = setTimeout(refreshPreview, ms == null ? 600 : ms);
}

async function refreshPreview() {
  const s = curScreen();
  const box = $("tubes");
  if (!s) { box.innerHTML = ""; return; }
  if (state.designerRefresh) state.designerRefresh();
  if (state.previewUrl) { URL.revokeObjectURL(state.previewUrl); state.previewUrl = null; }
  const { status, res, body } = await api("api/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ screen: s, font: state.data.font }),
  });
  let blob = null;
  if (status === 200) {
    try { blob = await res.blob(); } catch (e) { blob = null; }
  }
  // Even when the preview can't be drawn, keep six clickable tubes so a bad tube can be fixed
  const errs = status === 200 ? (res.headers.get("X-Preview-Errors") || "")
                              : ((body && (body.errors || []).join(" | ")) || (body && body.error) || "preview failed");
  if (errs) setStatus(errs, "error");
  state.previewUrl = blob ? URL.createObjectURL(blob) : null;
  box.innerHTML = "";
  for (let i = 0; i < 6; i++) {
    // One tube = one sixth of the 810x240 strip, scaled to whatever size the CSS gives the tube
    const img = document.createElement("div");
    img.className = "tube-img" + (i === state.selectedTube ? " selected" : "");
    if (state.previewUrl) {
      img.style.backgroundImage = "url(" + state.previewUrl + ")";
      img.style.backgroundPosition = (i * 20) + "% 0";
    } else {
      img.classList.add("broken");
      img.textContent = "tap to edit";
    }
    img.title = "tube " + i;
    img.addEventListener("click", () => {
      if (curScreen().strip || curScreen().canvas) return;   // one design: nothing per tube to edit
      if (state.selectedTube !== i) state.designerSel = null;
      state.selectedTube = i; renderTubeEditor(); markSelectedTube();
    });
    box.appendChild(img);
  }
  scheduleMirrorPost();      // while "Show on clock" is on, every preview refresh re-POSTs
}

function markSelectedTube() {
  document.querySelectorAll(".tube-img").forEach((img, i) => {
    img.classList.toggle("selected", i === state.selectedTube);
  });
}

$("refreshPreview").addEventListener("click", () => refreshPreview());

// ---------------------------------------------------------------- live mirror ("Show on clock")

// While on, the current screen is POSTed to api/mirror after every preview refresh
// (at most once per second, trailing call guaranteed) and re-POSTed every 60 s to
// keep it alive. Turning it off, hiding or closing the editor ends the mirror.
const mirror = { on: false, lastPost: 0, pending: null, keepAlive: null };

// with 2+ clocks: the clock chosen in the clock picker; with one clock: that clock;
// with "All clocks" chosen: no "clock" field, so the server uses the first clock
function mirrorClockName() {
  if (state.selectedClock) return state.selectedClock;
  if (state.clocks.length === 1) return state.clocks[0].name;
  return null;
}

function renderMirrorBtn() {
  const btn = $("mirrorBtn");
  btn.classList.toggle("hidden", state.clocks.length === 0);
  btn.classList.toggle("primary", mirror.on);   // accent colour while showing
  btn.textContent = mirror.on ? "Showing on clock" : "Show on clock";
}

async function postMirror() {
  const s = curScreen();
  if (!s || !mirror.on) return;
  mirror.lastPost = Date.now();
  const payload = { screen: s, seconds: 120 };
  const clock = mirrorClockName();
  if (clock) payload.clock = clock;
  await api("api/mirror", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

// at most one POST per second, with a trailing call guaranteed
function scheduleMirrorPost() {
  if (!mirror.on) return;
  const wait = Math.max(0, 1000 - (Date.now() - mirror.lastPost));
  clearTimeout(mirror.pending);
  mirror.pending = setTimeout(async () => {
    mirror.pending = null;
    await postMirror();
  }, wait);
}

async function mirrorOff() {
  if (!mirror.on) return;
  mirror.on = false;
  clearTimeout(mirror.pending);
  clearInterval(mirror.keepAlive);
  mirror.keepAlive = null;
  renderMirrorBtn();
  const payload = {};
  const clock = mirrorClockName();
  if (clock) payload.clock = clock;
  try {
    await api("api/mirror", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (e) { /* the clock may be gone; the mirror expires on its own */ }
}

$("mirrorBtn").addEventListener("click", async () => {
  if (mirror.on) { await mirrorOff(); return; }
  mirror.on = true;
  renderMirrorBtn();
  await postMirror();       // show right away, not after the first preview refresh
  mirror.keepAlive = setInterval(() => { if (mirror.on) postMirror(); }, 60000);
});

// hidden editor: stop mirroring (the clock keeps the last picture until it expires)
document.addEventListener("visibilitychange", () => {
  if (document.hidden) mirrorOff();
});
// closed editor: sendBeacon cannot DELETE, so POST the api/mirror/end alias too
window.addEventListener("pagehide", () => {
  if (!mirror.on) return;
  mirror.on = false;
  const payload = {};
  const clock = mirrorClockName();
  if (clock) payload.clock = clock;
  try {
    navigator.sendBeacon("api/mirror/end",
                         new Blob([JSON.stringify(payload)], { type: "application/json" }));
  } catch (e) { /* nothing more we can do while unloading */ }
});

// ---------------------------------------------------------------- tube editor

function renderTubeEditor() {
  const panel = $("tubeEditor");
  const s = curScreen();
  state.designerRefresh = null;
  if (s && s.strip) {
    // One picture across all six tubes: choose it and drag it into place
    panel.classList.remove("hidden");
    $("tubeTypeRow").classList.add("hidden");
    $("tubeTitle").textContent = "Picture across all six tubes";
    const box = $("tubeFields");
    box.innerHTML = "";
    box.appendChild(imageSourceField(s.strip, "source", 6 * TUBE_W, TUBE_H));
    return;
  }
  if (s && s.canvas) {
    // A free design across all six tubes: the custom-tube designer at strip size
    panel.classList.remove("hidden");
    $("tubeTypeRow").classList.add("hidden");
    $("tubeTitle").textContent = "Canvas across all six tubes";
    const box = $("tubeFields");
    box.innerHTML = "";
    box.appendChild(customEditor(s.canvas, 6 * TUBE_W, TUBE_H));
    return;
  }
  if (!s || state.selectedTube == null || !s.tubes) { panel.classList.add("hidden"); return; }
  panel.classList.remove("hidden");
  $("tubeTypeRow").classList.remove("hidden");
  const w = s.tubes[state.selectedTube] || { type: "blank" };
  $("tubeTitle").textContent = "Tube " + state.selectedTube + " — " + (w.type || "?");
  const sel = $("tType");
  if (!sel.options.length) {
    WIDGET_TYPES.forEach((t) => {
      const o = document.createElement("option");
      o.value = t; o.textContent = t;
      sel.appendChild(o);
    });
  }
  sel.value = w.type || "blank";
  buildTubeFields(w);
  markSelectedTube();
}

function buildTubeFields(w) {
  const box = $("tubeFields");
  box.innerHTML = "";
  const type = w.type || "blank";
  if (type === "custom") { box.appendChild(customEditor(w)); return; }
  (FIELDS[type] || []).forEach((f) => box.appendChild(fieldRow(w, f)));
  const more = MORE[type] || [];
  if (more.length) {
    const d = document.createElement("details");
    d.className = "more field-grid";
    d.open = more.some((f) => w[f] != null);
    const sum = document.createElement("summary");
    sum.textContent = "More options (sizes, colours)";
    d.appendChild(sum);
    more.forEach((f) => d.appendChild(fieldRow(w, f)));
    box.appendChild(d);
  }
  if (type !== "blank") {
    const hint = document.createElement("p");
    hint.className = "hint";
    if (type === "gif") {
      hint.textContent = "Plays on the clock (HA Tubes 2.4+): the whole GIF animates on its tube; " +
        "here and on older firmware you see its first frame. At most 2 gif tubes per screen.";
    } else {
      hint.textContent = "Need something this type can't do? Change the type to custom: the tube keeps its " +
        "contents as separate pieces you can move, resize and add to.";
    }
    box.appendChild(hint);
  }
}

function fieldRow(w, f, opts) {
  const row = document.createElement("div");
  row.className = "field-row";
  if (VALUE_FIELDS.includes(f)) {
    row.appendChild(valueSourceEditor(w, f));
  } else if (isColorField(f)) {
    row.appendChild(colorEditor(w, f));
  } else if (BOOL_FIELDS.includes(f)) {
    // a checkbox; the default (true for show_value/seconds, false for numerals) is not written
    const def = f === "numerals" ? false : true;
    const lab = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = w[f] != null ? !!w[f] : def;
    cb.addEventListener("change", () => {
      if (cb.checked === def) delete w[f]; else w[f] = cb.checked;
      markDirty(); schedulePreview();
    });
    lab.append(cb, document.createTextNode(" " + f.replace(/_/g, " ")));
    row.appendChild(lab);
  } else if (f === "unit_newline") {
    const lab = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = !!w[f];
    cb.addEventListener("change", () => { if (cb.checked) w[f] = true; else delete w[f]; markDirty(); schedulePreview(); });
    lab.append(cb, document.createTextNode(" unit on new line"));
    row.appendChild(lab);
  } else if (SELECTS[f]) {
    row.appendChild(selectField(w, f, SELECTS[f]));
  } else if (f === "font") {
    row.appendChild(fontField(w, f));
  } else if (f === "icon") {
    row.appendChild(iconField(w, f));
  } else if (f === "source") {
    row.appendChild(imageSourceField(w, f, (opts && opts.cw) || TUBE_W, (opts && opts.ch) || TUBE_H,
                                     w.type === "gif"));
  } else {
    row.appendChild(smartField(w, f));
  }
  return row;
}

function labelEl(text) {
  const l = document.createElement("span");
  l.className = "field-label"; l.textContent = text;
  return l;
}

// ---- icon: text box with Material Design Icons suggestions as you type ------------------
let iconListSeq = 0;
function iconField(w, f) {
  const wrap = document.createElement("label");
  wrap.className = "field";
  const listId = "iconList" + (++iconListSeq);
  const input = document.createElement("input");
  input.type = "text"; input.setAttribute("list", listId);
  input.placeholder = "mdi:train"; input.autocomplete = "off";
  input.value = w[f] != null ? String(w[f]) : "";
  const list = document.createElement("datalist");
  list.id = listId;
  const glyphHint = document.createElement("span");
  glyphHint.className = "status";
  let timer = null;
  async function suggest() {
    const { body } = await api("api/icons?q=" + encodeURIComponent(input.value));
    list.innerHTML = "";
    ((body && body.icons) || []).forEach((n) => {
      const o = document.createElement("option"); o.value = n; list.appendChild(o);
    });
  }
  input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(suggest, 200); });
  input.addEventListener("focus", suggest);
  input.addEventListener("change", () => {
    let v = input.value.trim().toLowerCase();
    if (v && !v.startsWith("mdi:")) v = "mdi:" + v;   // "train" -> "mdi:train"
    input.value = v;
    if (v) w[f] = v; else delete w[f];
    markDirty(); schedulePreview();
  });
  wrap.append(labelEl(f), input, list, glyphHint);
  return wrap;
}

// ---- image source: picture library (file:<name>), Claude logo, or anything typed in ----------
// gifOnly: the gif widget - the picker is filtered to .gif files
function imageSourceField(w, f, cw, ch, gifOnly) {
  const wrap = document.createElement("div");
  wrap.className = "field wide";
  const grid = document.createElement("div");
  grid.className = "image-grid";
  const input = document.createElement("input");
  input.type = "text";
  input.placeholder = gifOnly ? "file:name.gif (from the picture library)"
                              : "file:name.png, builtin:claude, camera:camera.x, https://...";
  input.value = w[f] != null ? String(w[f]) : "";
  const msg = document.createElement("span");
  msg.className = "status";

  const place = document.createElement("div");
  function drawPlace() {
    place.innerHTML = "";
    if (w[f] && w[f] !== "builtin:claude" && !gifOnly) place.appendChild(positioner(w, f, cw, ch));
  }

  function choose(src) {
    w[f] = src; input.value = src;
    // a different picture starts whole and centred
    ["zoom", "offset_x", "offset_y", "rotate"].forEach((k) => delete w[k]);
    if (src !== "builtin:claude" && !gifOnly) w.fit = "contain";
    markDirty(); schedulePreview(); drawGrid(); drawPlace();
  }

  function tile(label, src, thumbUrl, deletable) {
    const t = document.createElement("div");
    t.className = "image-tile" + (w[f] === src ? " selected" : "");
    t.title = src;
    if (thumbUrl) {
      const im = document.createElement("img");
      im.src = thumbUrl; im.alt = label;
      t.appendChild(im);
    }
    const cap = document.createElement("span");
    cap.textContent = label;
    t.appendChild(cap);
    t.addEventListener("click", () => choose(src));
    if (deletable) {
      const del = document.createElement("button");
      del.className = "image-del"; del.textContent = "×"; del.title = "Delete picture";
      del.addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!confirm("Delete " + label + "? Tubes that use it will show an error.")) return;
        await fetch("api/images/" + encodeURIComponent(label), { method: "DELETE" });
        drawGrid();
      });
      t.appendChild(del);
    }
    return t;
  }

  async function drawGrid() {
    const { body } = await api("api/images");
    grid.innerHTML = "";
    if (!gifOnly) {
      const claude = tile("Claude logo", "builtin:claude", null, false);
      const glyph = document.createElement("div");
      glyph.className = "builtin-thumb"; glyph.textContent = "\u2733";   // stands in for the starburst
      claude.insertBefore(glyph, claude.firstChild);
      grid.appendChild(claude);
    }
    ((body && body.images) || [])
      .filter((n) => !gifOnly || n.toLowerCase().endsWith(".gif"))
      .forEach((n) =>
        grid.appendChild(tile(n, "file:" + n, "api/images/" + encodeURIComponent(n), true)));
    const up = document.createElement("label");
    up.className = "image-tile upload";
    up.textContent = "+ Upload";
    const file = document.createElement("input");
    file.type = "file"; file.accept = gifOnly ? ".gif,image/gif" : "image/*"; file.hidden = true;
    file.addEventListener("change", async () => {
      if (!file.files.length) return;
      const fd = new FormData();
      fd.append("file", file.files[0]);
      msg.textContent = "uploading...";
      const { status, body: b } = await api("api/images", { method: "POST", body: fd });
      if (status === 200 && b && b.source) { msg.textContent = ""; choose(b.source); }
      else msg.textContent = (b && b.error) || "upload failed";
    });
    up.appendChild(file);
    grid.appendChild(up);
  }

  input.addEventListener("change", () => {
    if (input.value) w[f] = input.value; else delete w[f];
    markDirty(); schedulePreview(); drawGrid(); drawPlace();
  });
  wrap.append(labelEl("picture"), grid, input, msg, place);
  drawGrid(); drawPlace();
  return wrap;
}

// Any other field: fixed text or number, or a template (its live result shows underneath)
function smartField(w, f) {
  const wrap = document.createElement("div");
  wrap.className = "field smart";
  const multi = MULTILINE.includes(f);
  const input = document.createElement(multi ? "textarea" : "input");
  const cur = w[f] != null && w[f] !== "auto" ? String(w[f]) : "";
  if (multi) input.rows = Math.max(1, cur.split("\n").length); else input.type = "text";
  input.value = cur;
  if (f === "entity") input.dataset.entity = "1";
  const hint = (f === "width" || f === "height") && w.type !== "text" ? "" : HINTS[f];
  input.placeholder = hint || (multi ? "text or {{ template }}; Enter for a new line" : "");
  const out = document.createElement("div");
  out.className = "template-result";
  let timer = null;
  async function live() {
    const t = input.value;
    if (!hasTemplate(t)) { out.textContent = ""; return; }
    const { body } = await api("api/template", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ template: t }),
    });
    out.textContent = body && body.result != null ? "→ " + body.result : (body && body.error ? "error: " + body.error : "");
  }
  input.addEventListener("input", () => {
    const v = input.value;
    if (multi) input.rows = Math.max(1, v.split("\n").length);
    if (v.trim() === "") delete w[f];
    else if (!multi && /^-?\d+(\.\d+)?$/.test(v.trim())) w[f] = Number(v.trim());
    else w[f] = v;
    markDirty(); schedulePreview();
    clearTimeout(timer); timer = setTimeout(live, 400);
  });
  const line = document.createElement("label");
  line.className = "field";
  line.append(labelEl(f.replace(/_/g, " ")), input);
  wrap.append(line, out);
  live();
  return wrap;
}

function selectField(w, f, options) {
  const wrap = document.createElement("label");
  wrap.className = "field";
  const sel = document.createElement("select");
  options.forEach((o) => {
    const opt = document.createElement("option");
    opt.value = o; opt.textContent = o === "" ? "(default)" : o;
    sel.appendChild(opt);
  });
  sel.value = w[f] != null ? String(w[f]) : options[0];
  sel.addEventListener("change", () => {
    if (sel.value === options[0]) delete w[f]; else w[f] = sel.value;
    markDirty(); schedulePreview();
  });
  wrap.append(labelEl(f.replace(/_/g, " ")), sel);
  return wrap;
}

// ---- font picker: Default, the bundled families, your uploaded fonts, + Upload --------
let fontListPromise = null;
function fontList() {
  if (!fontListPromise) {
    fontListPromise = api("api/fonts").then(({ status, body }) =>
      status === 200 && body ? body : { bundled: [{ id: "default", name: "Default" }], files: [] });
  }
  return fontListPromise;
}

function fontField(w, f) {
  const wrap = document.createElement("div");
  wrap.className = "field font-field";
  const sel = document.createElement("select");
  const preview = document.createElement("img");
  preview.className = "font-preview-img";
  preview.alt = "font preview";
  const upload = document.createElement("input");
  upload.type = "file"; upload.accept = ".ttf,.otf"; upload.className = "hidden";
  const uploadBtn = mkBtn("+ Upload font", () => upload.click(), "upload a .ttf/.otf font (max 3 MB)");
  uploadBtn.classList.add("small");

  function previewUrl(fam) {
    return "api/font_preview?font=" + encodeURIComponent(fam || "default") +
      "&text=" + encodeURIComponent("Ag 123") + "&size=28";
  }
  function updatePreview() {
    const v = sel.value || "default";
    preview.src = previewUrl(v);
  }
  async function fill() {
    const { bundled, files } = await fontList();
    sel.innerHTML = "";
    const def = document.createElement("option");
    def.value = "default"; def.textContent = "Default";
    sel.appendChild(def);
    (bundled || []).forEach((b) => {
      if (b.id === "default") return;
      const o = document.createElement("option");
      o.value = b.id; o.textContent = b.name;
      sel.appendChild(o);
    });
    if (files && files.length) {
      const grp = document.createElement("optgroup");
      grp.label = "Your fonts";
      files.forEach((fl) => {
        const o = document.createElement("option");
        o.value = "file:" + fl.name; o.textContent = fl.name;
        grp.appendChild(o);
      });
      sel.appendChild(grp);
    }
    const cur = w[f] != null ? String(w[f]) : "default";
    if ([...sel.options].some((o) => o.value === cur)) sel.value = cur;
    else if (cur.startsWith("file:")) {
      // an uploaded font that was deleted since: keep it selectable so nothing is lost silently
      const o = document.createElement("option");
      o.value = cur; o.textContent = cur.slice(5) + " (missing)";
      sel.appendChild(o); sel.value = cur;
    }
    updatePreview();
  }
  sel.addEventListener("change", () => {
    if (sel.value === "default") delete w[f]; else w[f] = sel.value;
    updatePreview();
    markDirty(); schedulePreview();
  });
  upload.addEventListener("change", async () => {
    if (!upload.files || !upload.files[0]) return;
    const fd = new FormData();
    fd.append("file", upload.files[0]);
    const { status, body } = await api("api/fonts", { method: "POST", body: fd });
    if (status !== 200) { setStatus((body && body.error) || "font upload failed", "error"); return; }
    fontListPromise = null;               // re-fetch so the new font appears
    await fill();
    w[f] = body.family;                   // "file:<name>"
    sel.value = body.family;
    updatePreview();
    markDirty(); schedulePreview();
    upload.value = "";
  });
  const line = document.createElement("label");
  line.className = "field";
  line.append(labelEl("font"), sel);
  wrap.append(line, preview, uploadBtn, upload);
  fill();
  return wrap;
}

// ---- picture placement: drag to move, pinch / wheel / slider to zoom -------------------
// Writes fit: contain plus zoom, offset_x, offset_y, rotate and background, which render.py's
// _fit_image reads; the drawing here uses the same sums, so what you see is what the tube gets.
function positioner(w, f, cw, ch) {
  const wrap = document.createElement("div");
  wrap.className = "positioner";
  const canvas = document.createElement("canvas");
  const S = cw > 400 ? 1 : 2;
  canvas.width = cw * S; canvas.height = ch * S;
  canvas.className = "pos-canvas" + (cw > 400 ? " wide" : "");
  canvas.style.aspectRatio = cw + " / " + ch;
  if (cw <= 400) canvas.style.width = Math.min(270, cw * 2) + "px";
  const g = canvas.getContext("2d");
  const msg = document.createElement("div");
  msg.className = "status";
  msg.textContent = "loading picture…";
  let img = null, zoom = 1, ox = 0, oy = 0, rot = 0;

  const num = (v, d) => (typeof v === "number" && isFinite(v) ? v : (v != null && v !== "" && isFinite(Number(v)) ? Number(v) : d));
  const hex = (v) => (/^#[0-9a-fA-F]{6}$/.test(v || "") ? v : null);
  const screenBg = () => hex((curScreen() || {}).background) || "#000000";
  const bgNow = () => hex(w.background) || screenBg();

  function turned() {         // the picture's size once rotated
    const a = rot * Math.PI / 180, c = Math.abs(Math.cos(a)), sn = Math.abs(Math.sin(a));
    return [img.width * c + img.height * sn, img.width * sn + img.height * c];
  }
  const contain = () => { const [sw, sh] = turned(); return Math.min(cw / sw, ch / sh); };
  const cover = () => { const [sw, sh] = turned(); return Math.max(cw / sw, ch / sh); };

  function read() {
    rot = num(w.rotate, 0);
    zoom = num(w.zoom, 1);
    if ((w.fit || "cover") !== "contain") zoom *= cover() / contain();
    ox = num(w.offset_x, 0); oy = num(w.offset_y, 0);
  }

  function draw() {
    g.setTransform(1, 0, 0, 1, 0, 0);
    g.fillStyle = bgNow();
    g.fillRect(0, 0, canvas.width, canvas.height);
    if (!img) return;
    g.setTransform(S, 0, 0, S, 0, 0);
    g.save();
    g.translate(cw / 2 + ox, ch / 2 + oy);
    g.rotate(-rot * Math.PI / 180);
    const sc = contain() * zoom;
    g.imageSmoothingQuality = "high";
    g.drawImage(img, -img.width * sc / 2, -img.height * sc / 2, img.width * sc, img.height * sc);
    g.restore();
    if (cw > TUBE_W) {        // where one tube ends and the next begins
      g.strokeStyle = "rgba(255,255,255,0.45)"; g.lineWidth = 1; g.setLineDash([4, 4]);
      for (let x = TUBE_W; x < cw; x += TUBE_W) { g.beginPath(); g.moveTo(x + 0.5, 0); g.lineTo(x + 0.5, ch); g.stroke(); }
      g.setLineDash([]);
    }
    slider.value = Math.log2(zoom);
    zoomOut.textContent = Math.round(zoom * 100) + "%";
  }

  function commit() {
    zoom = Math.max(0.05, Math.min(20, zoom));
    w.fit = "contain";
    const z = Math.round(zoom * 1000) / 1000;
    if (z !== 1) w.zoom = z; else delete w.zoom;
    if (Math.round(ox)) w.offset_x = Math.round(ox); else delete w.offset_x;
    if (Math.round(oy)) w.offset_y = Math.round(oy); else delete w.offset_y;
    const r = ((Math.round(rot) % 360) + 360) % 360;
    if (r) w.rotate = r; else delete w.rotate;
    draw(); markDirty(); schedulePreview();
  }

  // one finger / mouse drags, two fingers pinch
  const pts = new Map();
  let pinch = null;
  const scale = () => cw / canvas.getBoundingClientRect().width;   // screen px -> tube px
  canvas.addEventListener("pointerdown", (e) => {
    if (!img) return;
    canvas.setPointerCapture(e.pointerId);
    pts.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pts.size === 2) {
      const [a, b] = [...pts.values()];
      pinch = { d: Math.hypot(a.x - b.x, a.y - b.y) || 1, zoom };
    }
  });
  canvas.addEventListener("pointermove", (e) => {
    const p = pts.get(e.pointerId);
    if (!p) return;
    if (pts.size === 1) {
      ox += (e.clientX - p.x) * scale(); oy += (e.clientY - p.y) * scale();
    }
    p.x = e.clientX; p.y = e.clientY;
    if (pts.size === 2 && pinch) {
      const [a, b] = [...pts.values()];
      zoom = Math.max(0.05, Math.min(20, pinch.zoom * Math.hypot(a.x - b.x, a.y - b.y) / pinch.d));
    }
    draw();
  });
  const up = (e) => {
    if (!pts.has(e.pointerId)) return;
    pts.delete(e.pointerId);
    if (pts.size < 2) pinch = null;
    if (pts.size === 0) commit();
  };
  canvas.addEventListener("pointerup", up);
  canvas.addEventListener("pointercancel", up);
  let wheelTimer = null;
  canvas.addEventListener("wheel", (e) => {
    if (!img) return;
    e.preventDefault();
    zoom = Math.max(0.05, Math.min(20, zoom * Math.exp(-e.deltaY * 0.0015)));
    draw();
    clearTimeout(wheelTimer); wheelTimer = setTimeout(commit, 250);
  }, { passive: false });

  const controls = document.createElement("div");
  controls.className = "pos-controls";
  const slider = document.createElement("input");
  slider.type = "range"; slider.min = "-4"; slider.max = "4"; slider.step = "0.01"; slider.value = "0";
  slider.title = "zoom";
  slider.addEventListener("input", () => { zoom = Math.pow(2, parseFloat(slider.value)); draw(); });
  slider.addEventListener("change", commit);
  const zoomOut = document.createElement("span");
  zoomOut.className = "unit";
  const zoomRow = document.createElement("label");
  zoomRow.className = "field";
  zoomRow.append(labelEl("zoom"), slider, zoomOut);
  const buttons = document.createElement("div");
  buttons.className = "row";
  buttons.append(
    mkBtn("Whole picture", () => { zoom = 1; ox = 0; oy = 0; commit(); }, "show all of the picture"),
    mkBtn("Fill", () => { zoom = cover() / contain(); ox = 0; oy = 0; commit(); }, "fill the tube, cropping the picture"),
    mkBtn("Centre", () => { ox = 0; oy = 0; commit(); }),
    mkBtn("Rotate", () => {
      const fill = Math.abs(zoom - cover() / contain()) < 0.002;
      rot = (Math.round(rot / 90) * 90 + 270) % 360;      // a quarter turn clockwise
      if (fill) zoom = cover() / contain();
      commit();
    }, "turn a quarter turn"),
  );
  const bg = document.createElement("input");
  bg.type = "color"; bg.value = bgNow();
  bg.addEventListener("input", () => { w.background = bg.value; draw(); markDirty(); schedulePreview(); });
  const bgRow = document.createElement("label");
  bgRow.className = "field";
  bgRow.append(labelEl("background"), bg,
    mkBtn("same as screen", (e) => { e.preventDefault(); delete w.background; bg.value = bgNow(); draw(); markDirty(); schedulePreview(); }));
  controls.append(zoomRow, buttons, bgRow);
  [slider, bg, ...buttons.querySelectorAll("button")].forEach((el) => { el.disabled = true; });

  (async function load() {
    const { status, res, body } = await api("api/source_image", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: w[f] }),
    });
    if (status !== 200) { msg.textContent = "can't show this picture here: " + ((body && body.error) || status); return; }
    const url = URL.createObjectURL(await res.blob());
    const im = new Image();
    im.onload = () => {
      img = im; read(); draw();
      msg.textContent = "Drag the picture to move it. Pinch, scroll or use the slider to zoom.";
      [slider, bg, ...buttons.querySelectorAll("button")].forEach((el) => { el.disabled = false; });
    };
    im.onerror = () => { msg.textContent = "can't show this picture here"; };
    im.src = url;
  })();

  draw();
  wrap.append(labelEl("position"), canvas, msg, controls);
  return wrap;
}

// ---- custom tube / canvas designer ------------------------------------------------------
// The tube (or the whole 810x240 canvas) is drawn by the app (so fonts and sizes are exact);
// the outlines over it are where each item ended up (X-Item-Boxes from api/preview) and can
// be dragged. cw/ch are the canvas size: 135x240 for a tube, 810x240 for a canvas screen.
function customEditor(w, cw, ch) {
  cw = cw || TUBE_W; ch = ch || TUBE_H;
  const wide = cw > TUBE_W;
  if (!Array.isArray(w.items)) w.items = [];
  const tube = state.selectedTube;
  const wrap = document.createElement("div");
  wrap.className = "designer";
  const stageScroll = document.createElement("div");
  stageScroll.className = "designer-stage-scroll";
  const stage = document.createElement("div");
  stage.className = "designer-stage" + (wide ? " wide" : "");
  const pic = document.createElement("img");
  pic.alt = "preview"; pic.draggable = false;
  const overlay = document.createElement("div");
  overlay.className = "designer-overlay";
  stage.append(pic, overlay);
  stageScroll.appendChild(stage);
  const side = document.createElement("div");
  side.className = "designer-side";
  const addRow = document.createElement("div");
  addRow.className = "row";
  const list = document.createElement("ul");
  list.className = "item-list";
  const fields = document.createElement("div");
  fields.className = "field-grid";
  side.append(addRow, list, fields);
  wrap.append(stageScroll, side);

  const numOr = (v, d) => (typeof v === "number" ? v : (v != null && v !== "" && isFinite(Number(v)) ? Number(v) : d));
  let boxes = [], seq = 0, picUrl = null, dragging = false;
  // snap to grid: remembered in the browser; the grid is measured from the canvas centre
  const CX = Math.round(cw / 2), CY = ch / 2;
  const snapCb = document.createElement("input");
  snapCb.type = "checkbox"; snapCb.checked = localStorage.getItem("tdSnap") === "1";
  const snapSize = document.createElement("select");
  [2, 5, 10, 15, 20].forEach((n) => { const o = document.createElement("option"); o.value = n; o.textContent = n + " px"; snapSize.appendChild(o); });
  snapSize.value = localStorage.getItem("tdSnapSize") || "5";
  const grid = () => (snapCb.checked ? parseInt(snapSize.value, 10) : 0);
  // on a canvas, also offer snapping x to the centres of the six physical tubes
  const tubeCb = document.createElement("input");
  tubeCb.type = "checkbox"; tubeCb.checked = localStorage.getItem("tdSnapTube") === "1";
  const snapTube = () => (wide && tubeCb.checked);
  function drawGrid() {
    const g = grid(), k = stage.getBoundingClientRect().width / cw;
    if (!g || !k) { overlay.style.backgroundImage = ""; return; }
    const line = "rgba(255,255,255,0.13) 1px, transparent 1px";
    overlay.style.backgroundImage = "linear-gradient(to right, " + line + "), linear-gradient(to bottom, " + line + ")";
    overlay.style.backgroundSize = (g * k) + "px " + (g * k) + "px";
    overlay.style.backgroundPosition = ((CX * k) % (g * k)) + "px " + ((CY * k) % (g * k)) + "px";
  }
  function drawTubeLines() {
    overlay.querySelectorAll(".tube-line").forEach((el) => el.remove());
    if (!wide) return;
    for (let t = 1; t < 6; t++) {
      const el = document.createElement("div");
      el.className = "tube-line";
      el.style.left = (t * TUBE_W / cw * 100) + "%";
      el.title = "gap between tube " + t + " and tube " + (t + 1);
      overlay.appendChild(el);
    }
  }
  const snapChanged = () => {
    localStorage.setItem("tdSnap", snapCb.checked ? "1" : "0");
    localStorage.setItem("tdSnapSize", snapSize.value);
    localStorage.setItem("tdSnapTube", tubeCb.checked ? "1" : "0");
    drawGrid();
  };
  snapCb.addEventListener("change", snapChanged);
  snapSize.addEventListener("change", snapChanged);
  tubeCb.addEventListener("change", snapChanged);
  const snapRow = document.createElement("label");
  snapRow.className = "field";
  snapRow.append(snapCb, document.createTextNode(" snap to grid "), snapSize);
  side.insertBefore(snapRow, list);
  if (wide) {
    const tubeRow = document.createElement("label");
    tubeRow.className = "field";
    tubeRow.append(tubeCb, document.createTextNode(" snap to tube centres"));
    side.insertBefore(tubeRow, list);
  }

  function summary(it) {
    const v = it.type === "text" ? it.text : it.type === "icon" ? it.icon : it.type === "image" ? it.source
      : (it.type === "ring" || it.type === "bar") ? it.value : "";
    const t = v == null ? "" : String(v).replace(/\s+/g, " ");
    return it.type + (t ? ": " + (t.length > 26 ? t.slice(0, 26) + "…" : t) : "");
  }

  function changed() { markDirty(); schedulePreview(0); }

  function select(i, keepFields) {
    state.designerSel = i;
    drawList(); drawBoxes();
    if (!keepFields) drawFields();
  }

  function drawList() {
    list.innerHTML = "";
    w.items.forEach((it, i) => {
      const li = document.createElement("li");
      li.className = "item-row" + (i === state.designerSel ? " selected" : "");
      const name = document.createElement("span");
      name.className = "rot-name"; name.textContent = summary(it);
      name.addEventListener("click", () => select(i));
      const move = (d) => () => {
        const j = i + d;
        if (j < 0 || j >= w.items.length) return;
        [w.items[i], w.items[j]] = [w.items[j], w.items[i]];
        state.designerSel = j; drawList(); changed();
      };
      li.append(name,
        mkBtn("↑", move(-1), "further back"), mkBtn("↓", move(1), "further forward"),
        mkBtn("⧉", () => {
          const copy = JSON.parse(JSON.stringify(it));
          if (typeof copy.y === "number") copy.y += 16;
          w.items.splice(i + 1, 0, copy); state.designerSel = i + 1; drawList(); drawFields(); changed();
        }, "duplicate"),
        mkBtn("×", () => {
          w.items.splice(i, 1);
          state.designerSel = w.items.length ? Math.min(i, w.items.length - 1) : null;
          drawList(); drawFields(); changed();
        }, "delete"));
      list.appendChild(li);
    });
    if (!w.items.length) {
      const li = document.createElement("li");
      li.className = "hint"; li.textContent = "Empty " + (wide ? "canvas" : "tube") + ". Add something with the buttons above.";
      list.appendChild(li);
    }
  }

  function drawFields() {
    fields.innerHTML = "";
    const it = w.items[state.designerSel];
    if (!it) return;
    const head = document.createElement("h3");
    head.textContent = it.type + (it.type === "text" && (it.align === "left" || it.align === "right")
      ? " (x is its " + it.align + " edge)" : "");
    fields.appendChild(head);
    (ITEM_FIELDS[it.type] || []).forEach((f) => {
      fields.appendChild(fieldRow(it, f, { cw: Math.round(numOr(it.width, cw)), ch: Math.round(numOr(it.height, ch)) }));
    });
  }

  function drawBoxes() {
    overlay.querySelectorAll(".item-box").forEach((el) => el.remove());
    boxes.forEach((b, i) => {
      if (!w.items[i]) return;
      const el = document.createElement("div");
      el.className = "item-box" + (i === state.designerSel ? " selected" : "");
      el.style.left = (b[0] / cw * 100) + "%";
      el.style.top = (b[1] / ch * 100) + "%";
      el.style.width = (Math.max(b[2] - b[0], 6) / cw * 100) + "%";
      el.style.height = (Math.max(b[3] - b[1], 6) / ch * 100) + "%";
      el.title = summary(w.items[i]);
      el.addEventListener("pointerdown", (e) => startDrag(e, el, i));
      overlay.appendChild(el);
    });
    drawTubeLines();
  }

  function startDrag(e, el, i) {
    e.preventDefault();
    const it = w.items[i];
    if (state.designerSel !== i) { state.designerSel = i; drawList(); drawFields(); }
    overlay.querySelectorAll(".item-box").forEach((b) => b.classList.toggle("selected", b === el));
    // a template for x or y decides the place itself; such an item can't be dragged
    if (hasTemplate(it.x) || hasTemplate(it.y)) return;
    el.setPointerCapture(e.pointerId);
    dragging = true;
    const k = cw / stage.getBoundingClientRect().width;
    const sx = e.clientX, sy = e.clientY;
    let dx = 0, dy = 0;
    const move = (ev) => {
      dx = (ev.clientX - sx) * k; dy = (ev.clientY - sy) * k;
      el.style.transform = "translate(" + (dx / k) + "px," + (dy / k) + "px)";
    };
    const done = () => {
      el.removeEventListener("pointermove", move);
      el.removeEventListener("pointerup", done);
      el.removeEventListener("pointercancel", done);
      dragging = false;
      if (Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5) { drawBoxes(); return; }
      const g = grid() || 1;
      let nx = CX + Math.round((numOr(it.x, CX) + dx - CX) / g) * g;
      if (snapTube()) {
        // nearest tube centre: 67.5 + t * 135
        const t = Math.round((nx - TUBE_W / 2) / TUBE_W);
        nx = Math.round(TUBE_W / 2 + t * TUBE_W);
      }
      it.x = nx;
      it.y = CY + Math.round((numOr(it.y, CY) + dy - CY) / g) * g;
      drawFields(); changed();
    };
    el.addEventListener("pointermove", move);
    el.addEventListener("pointerup", done);
    el.addEventListener("pointercancel", done);
  }

  async function refresh() {
    const s = curScreen();
    if (!s) return;
    if (wide ? s.canvas !== w : !s.tubes || s.tubes[tube] !== w) return;
    const mine = ++seq;
    const payload = { screen: s, font: state.data.font };
    if (!wide) payload.tube = tube;
    const { status, res } = await api("api/preview", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (mine !== seq || status !== 200) return;
    const blob = await res.blob();
    if (picUrl) URL.revokeObjectURL(picUrl);
    picUrl = URL.createObjectURL(blob);
    pic.src = picUrl;
    try { boxes = JSON.parse(res.headers.get("X-Item-Boxes") || "[]"); } catch (e) { boxes = []; }
    if (!dragging) drawBoxes();      // never swap the outline out from under a finger
    drawGrid();
  }

  Object.keys(ITEM_FIELDS).forEach((t) => {
    addRow.appendChild(mkBtn("+ " + (t === "rect" ? "box / line" : t === "image" ? "picture" : t), () => {
      w.items.push(itemNew(t, cw));
      state.designerSel = w.items.length - 1;
      drawList(); drawFields(); changed();
    }));
  });

  if (state.designerSel == null || !w.items[state.designerSel]) state.designerSel = w.items.length ? 0 : null;
  drawList(); drawFields(); drawTubeLines();
  state.designerRefresh = refresh;
  refresh();
  return wrap;
}

// A widget of another type as custom items laid out roughly the same, so nothing is lost when
// a tube's type is changed to custom.
function toCustom(w) {
  const items = [];
  const cx = Math.round(TUBE_W / 2);
  const text = (t, y, more) => { if (t != null && t !== "") items.push(Object.assign({ type: "text", text: t, x: cx, y }, more)); };
  const col = (c, more) => Object.assign({}, more, c != null ? { color: c } : {});
  const join = (a, b) => (b == null || b === "" ? a : String(a == null ? "" : a) + String(b));
  if (w.type === "text") {
    const align = w.align || "center";
    text(w.text, 120, col(w.color, Object.assign(
      { x: align === "left" ? 8 : align === "right" ? TUBE_W - 8 : cx, width: 119, height: 224 },
      w.size != null && w.size !== "auto" ? { size: w.size } : {},
      w.weight ? { weight: w.weight } : {}, align !== "center" ? { align } : {})));
  } else if (w.type === "big_number") {
    const big = { weight: "bold", width: 127 };
    if (w.unit && w.unit_newline) {
      text(w.value, 86, col(w.value_color || w.color, Object.assign({ height: 96 }, big, w.value_size ? { size: w.value_size } : {})));
      text(w.unit, 166, col(w.unit_color || w.color, Object.assign({ height: 30 }, big, w.unit_size ? { size: w.unit_size } : {})));
    } else {
      text(join(w.value, w.unit), w.label ? 106 : 120, col(w.value_color || w.color,
        Object.assign({ height: w.label ? 130 : 200 }, big, w.value_size ? { size: w.value_size } : {})));
    }
    text(w.label, 206, col(w.label_color || w.color, { size: w.label_size || 18 }));
  } else if (w.type === "ring") {
    const r = Number(w.radius) || 52, th = Number(w.thickness) || 12;
    items.push(col(w.color, Object.assign({ type: "ring", value: w.value, x: cx, y: 104, radius: r, thickness: th },
      w.max != null ? { max: w.max } : {})));
    const inner = 2 * (r - th);
    if (w.unit && w.unit_newline) {
      text(w.value, 96, col(w.value_color, { weight: "bold", width: inner, height: 34, size: w.value_size || "auto" }));
      text(w.unit, 128, col(w.unit_color || w.value_color, { weight: "bold", size: w.unit_size || 16 }));
    } else {
      text(join(w.value, w.unit), 104, col(w.value_color, { weight: "bold", width: inner, height: 30, size: w.value_size || "auto" }));
    }
    text(w.label, 104 + r + 18, col(w.label_color, { size: w.label_size || 16 }));
  } else if (w.type === "bar") {
    items.push(col(w.color, Object.assign({ type: "bar", value: w.value, x: cx, y: 104, width: 75, height: 160, radius: 4, direction: "up" },
      w.max != null ? { max: w.max } : {})));
    text(join(w.value, w.unit), 104, col(w.value_color, { weight: "bold", width: 70, height: 26, size: w.value_size || "auto" }));
    text(w.label, 210, col(w.label_color, { size: w.label_size || 16 }));
  } else if (w.type === "icon") {
    if (w.icon) items.push(col(w.color, { type: "icon", icon: w.icon, x: cx, y: w.text ? 88 : 120, size: w.icon_size || (w.text ? 96 : 120) }));
    text(w.text, 170, col(w.text_color, { width: 123, height: 60, size: w.text_size || 24 }));
  } else if (w.type === "image" && w.source) {
    const it = { type: "image", source: w.source, x: cx, y: 120, width: TUBE_W, height: TUBE_H, fit: w.fit || "cover" };
    ["zoom", "offset_x", "offset_y", "rotate", "background"].forEach((k) => { if (w[k] != null) it[k] = w[k]; });
    items.push(it);
  }
  return { type: "custom", items };
}

// ---- value source: Entity / Fixed / Template ------------------------------

const ENTITY_RE = /^\{\{\s*states\('([^']+)'\)\s*\|\s*float\(0\)\s*\|\s*(?:round\((\d)\)\s*\|\s*)?(int|round\((\d)\))\s*\}\}$/;

function valueSourceEditor(w, f) {
  const wrap = document.createElement("div");
  wrap.className = "value-source";
  const current = w[f] != null ? String(w[f]) : "";
  const m = current.match(ENTITY_RE);
  let mode = "template";
  if (m) mode = "entity";
  else if (current && !current.includes("{{") && !current.includes("{%")) mode = "fixed";

  const tabs = document.createElement("div");
  tabs.className = "tabs";
  const body = document.createElement("div");
  body.className = "tab-body";

  function setMode(newMode) {
    mode = newMode;
    [...tabs.children].forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
    buildBody();
  }

  ["entity", "fixed", "template"].forEach((mm) => {
    const b = document.createElement("button");
    b.type = "button"; b.className = "btn small tab"; b.dataset.mode = mm; b.textContent = mm;
    b.addEventListener("click", () => setMode(mm));
    tabs.appendChild(b);
  });

  function apply(value) {
    if (value === "" || value == null) delete w[f]; else w[f] = value;
    markDirty(); schedulePreview();
  }

  function buildBody() {
    body.innerHTML = "";
    if (mode === "entity") {
      const ent = m ? m[1] : "";
      // decimals: the last filter wins - "round(0) | int" and "int" both mean 0
      const dec = m ? (m[4] !== undefined ? parseInt(m[4], 10)
                    : m[3] === "int" ? 0 : parseInt(m[2], 10)) : 1;
      const row = document.createElement("div");
      row.className = "entity-picker";
      const input = document.createElement("input");
      input.type = "text"; input.placeholder = "search entity…"; input.value = ent;
      const list = document.createElement("div");
      list.className = "entity-list";
      const decSel = document.createElement("select");
      [0, 1, 2].forEach((d) => {
        const o = document.createElement("option");
        o.value = d; o.textContent = d + " decimals";
        decSel.appendChild(o);
      });
      decSel.value = dec;
      let timer = null;
      async function search() {
        const q = input.value.trim();
        const { status, body } = await api("api/entities?q=" + encodeURIComponent(q));
        list.innerHTML = "";
        if (status !== 200 || !Array.isArray(body)) return;
        body.forEach((e) => {
          const b = document.createElement("button");
          b.type = "button"; b.className = "entity-opt";
          b.textContent = e.entity_id + (e.name && e.name !== e.entity_id ? " (" + e.name + ")" : "") +
            (e.state != null ? " — " + e.state + (e.unit || "") : "");
          b.addEventListener("click", () => {
            input.value = e.entity_id;
            const d = parseInt(decSel.value, 10);
            apply("{{ states('" + e.entity_id + "') | float(0) | " + (d === 0 ? "int" : "round(" + d + ")") + " }}");
          });
          list.appendChild(b);
        });
      }
      input.addEventListener("input", () => {
        clearTimeout(timer);
        timer = setTimeout(() => {
          const v = input.value.trim();
          if (v.includes(".") && /^[a-z_]+\.[a-z0-9_]+$/.test(v)) {
            const d = parseInt(decSel.value, 10);
            apply("{{ states('" + v + "') | float(0) | " + (d === 0 ? "int" : "round(" + d + ")") + " }}");
          }
          search();
        }, 250);
      });
      decSel.addEventListener("change", () => {
        const v = input.value.trim();
        if (/^[a-z_]+\.[a-z0-9_]+$/.test(v)) {
          const d = parseInt(decSel.value, 10);
          apply("{{ states('" + v + "') | float(0) | " + (d === 0 ? "int" : "round(" + d + ")") + " }}");
        }
      });
      const lab = document.createElement("label");
      lab.className = "field";
      lab.append(labelEl(f), input, decSel);
      row.appendChild(lab);
      row.appendChild(list);
      body.appendChild(row);
      search();
    } else if (mode === "fixed") {
      const input = document.createElement("textarea");
      input.rows = Math.max(1, current.split("\n").length); input.value = hasTemplate(current) ? "" : current;
      input.placeholder = "Enter for a new line";
      input.addEventListener("input", () => {
        input.rows = Math.max(1, input.value.split("\n").length);
        apply(input.value);
      });
      const lab = document.createElement("label");
      lab.className = "field";
      lab.append(labelEl(f), input);
      body.appendChild(lab);
    } else {
      const ta = document.createElement("textarea");
      ta.rows = 3; ta.value = current;
      ta.placeholder = "{{ ... }} or {% ... %}";
      const out = document.createElement("div");
      out.className = "template-result";
      let timer = null;
      async function live() {
        const t = ta.value;
        if (!t.includes("{{") && !t.includes("{%")) { out.textContent = ""; return; }
        const { body } = await api("api/template", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ template: t }),
        });
        out.textContent = body && body.result != null ? "→ " + body.result : (body && body.error ? "error: " + body.error : "");
      }
      ta.addEventListener("input", () => {
        apply(ta.value);
        clearTimeout(timer); timer = setTimeout(live, 400);
      });
      const lab = document.createElement("label");
      lab.className = "field wide";
      lab.append(labelEl(f), ta);
      body.appendChild(lab);
      body.appendChild(out);
      live();
    }
  }

  setMode(mode);
  wrap.append(labelEl(f + " source"), tabs, body);
  return wrap;
}

// ---- colour: Fixed / Thresholds / Template --------------------------------

const THRESH_RE = /^\{%\s*set v = states\('([^']+)'\) \| float\(0\) %\}\{\{\s*'([^']*)' if v < ([0-9.]+) else '([^']*)' if v < ([0-9.]+) else '([^']*)'\s*\}\}$/;

function colorEditor(w, f) {
  const wrap = document.createElement("div");
  wrap.className = "value-source";
  const current = w[f] != null ? String(w[f]) : "";
  const m = current.match(THRESH_RE);
  let mode = "template";
  if (m) mode = "thresholds";
  else if (current === "" || /^#[0-9a-fA-F]{3,6}$/.test(current)) mode = "fixed";

  const tabs = document.createElement("div");
  tabs.className = "tabs";
  const body = document.createElement("div");
  body.className = "tab-body";

  function setMode(nm) {
    mode = nm;
    [...tabs.children].forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
    buildBody();
  }
  ["fixed", "thresholds", "template"].forEach((mm) => {
    const b = document.createElement("button");
    b.type = "button"; b.className = "btn small tab"; b.dataset.mode = mm; b.textContent = mm;
    b.addEventListener("click", () => setMode(mm));
    tabs.appendChild(b);
  });

  function apply(v) {
    if (v) w[f] = v; else delete w[f];
    markDirty(); schedulePreview();
  }

  function buildBody() {
    body.innerHTML = "";
    if (mode === "fixed") {
      const input = document.createElement("input");
      input.type = "color";
      // With no colour set, start from what the tube really shows (icons, rings, bars,
      // sparklines, graphs and progress arcs default to Claude orange), so picking white
      // is a real change that gets saved
      const fallback = f === "track" ? "#3c3c3c" : f !== "color" ? "#ffffff"
        : ["icon", "ring", "bar", "sparkline", "graph", "progress"].includes(w.type) ? "#d97757" : "#ffffff";
      input.value = /^#[0-9a-fA-F]{6}$/.test(current) ? current : fallback;
      input.addEventListener("input", () => apply(input.value));
      input.addEventListener("change", () => apply(input.value));
      const reset = mkBtn("default", () => { input.value = fallback; apply(""); }, "use the default colour");
      const lab = document.createElement("label");
      lab.className = "field";
      lab.append(labelEl(f.replace(/_/g, " ")), input, reset);
      body.appendChild(lab);
    } else if (mode === "thresholds") {
      const ent = m ? m[1] : "";
      const rows = m ? [[m[3], m[2]], [m[5], m[4]]] : [["", "#4CAF50"], ["", "#F44336"]];
      const otherwise = m ? m[6] : "#FFC107";
      const entInput = document.createElement("input");
      entInput.type = "text"; entInput.placeholder = "entity"; entInput.value = ent; entInput.dataset.entity = "1";
      const list = document.createElement("div");
      list.className = "thresh-rows";
      const rowEls = [];
      function rebuild() {
        const e = entInput.value.trim();
        const parts = rowEls.map((r) => "'" + r.color.value + "' if v < " + r.num.value);
        apply("{% set v = states('" + e + "') | float(0) %}{{ " +
          (parts.length ? parts.join(" else ") + " else " : "") + "'" + otherwiseInput.value + "' }}");
      }
      rows.forEach(([num, col]) => {
        const div = document.createElement("div");
        div.className = "thresh-row";
        const numIn = document.createElement("input");
        numIn.type = "number"; numIn.step = "any"; numIn.value = num; numIn.placeholder = "below";
        const colIn = document.createElement("input");
        colIn.type = "color"; colIn.value = /^#[0-9a-fA-F]{6}$/.test(col) ? col : "#4CAF50";
        numIn.addEventListener("change", rebuild);
        colIn.addEventListener("change", rebuild);
        div.append(document.createTextNode("below "), numIn, colIn);
        list.appendChild(div);
        rowEls.push({ num: numIn, color: colIn });
      });
      const otherwiseInput = document.createElement("input");
      otherwiseInput.type = "color";
      otherwiseInput.value = /^#[0-9a-fA-F]{6}$/.test(otherwise) ? otherwise : "#FFC107";
      otherwiseInput.addEventListener("change", rebuild);
      const odiv = document.createElement("div");
      odiv.className = "thresh-row";
      odiv.append(document.createTextNode("otherwise "), otherwiseInput);
      entInput.addEventListener("change", rebuild);
      const lab = document.createElement("label");
      lab.className = "field";
      lab.append(labelEl("entity"), entInput);
      body.appendChild(lab);
      body.appendChild(list);
      body.appendChild(odiv);
      // Opening a tube that already uses thresholds must not rewrite its template (the colour
      // pickers lower-case the colours, which would mark the screen changed); switching to the
      // thresholds tab does write one
      if (!(opening && m)) rebuild();
    } else {
      const ta = document.createElement("textarea");
      ta.rows = 2; ta.value = current;
      ta.placeholder = "#RRGGBB or {{ ... }} / {% ... %}";
      ta.addEventListener("change", () => apply(ta.value));
      const lab = document.createElement("label");
      lab.className = "field wide";
      lab.append(labelEl(f), ta);
      body.appendChild(lab);
    }
  }

  let opening = true;
  setMode(mode);
  opening = false;
  wrap.append(labelEl(f.replace(/_/g, " ") + " source"), tabs, body);
  return wrap;
}

// ---- tube buttons ---------------------------------------------------------

// Sensible starting values when a tube's type is switched to one of the new types
// (fields the old widget already had are kept by the generic loop above).
const TYPE_DEFAULTS = {
  graph:        { hours: 24, style: "area", show_value: true },
  progress:     { max: 100, eta_format: "remaining" },
  analog_clock: { seconds: true, numerals: false },
  weather:      { show: "today" },
  ticker:       { size: 28, y: 120, speed: 40 },
};

$("tType").addEventListener("change", (e) => {
  const s = curScreen(); if (!s || state.selectedTube == null) return;
  const w = s.tubes[state.selectedTube];
  const old = { ...w };
  let nw = { type: e.target.value };
  // keep fields that exist for the new type
  (FIELDS[e.target.value] || []).concat(MORE[e.target.value] || []).forEach((f) => { if (f in old) nw[f] = old[f]; });
  Object.assign(nw, JSON.parse(JSON.stringify(TYPE_DEFAULTS[e.target.value] || {})));
  if (e.target.value === "custom") nw = toCustom(old);
  state.designerSel = null;
  s.tubes[state.selectedTube] = nw;
  markDirty(); renderTubeEditor(); schedulePreview();
});

$("copyTube").addEventListener("click", () => {
  const s = curScreen(); if (!s || state.selectedTube == null) return;
  state.clipboard = JSON.parse(JSON.stringify(s.tubes[state.selectedTube]));
});
$("pasteTube").addEventListener("click", () => {
  const s = curScreen(); if (!s || state.selectedTube == null || !state.clipboard) return;
  s.tubes[state.selectedTube] = JSON.parse(JSON.stringify(state.clipboard));
  markDirty(); renderTubeEditor(); schedulePreview();
});
$("tubeLeft").addEventListener("click", () => moveTube(-1));
$("tubeRight").addEventListener("click", () => moveTube(1));

function moveTube(dir) {
  const s = curScreen(); if (!s || state.selectedTube == null) return;
  const j = state.selectedTube + dir;
  if (j < 0 || j >= 6) return;
  [s.tubes[state.selectedTube], s.tubes[j]] = [s.tubes[j], s.tubes[state.selectedTube]];
  state.selectedTube = j;
  markDirty(); renderTubeEditor(); schedulePreview();
}

// ---------------------------------------------------------------- save

$("saveBtn").addEventListener("click", async () => {
  setStatus("saving…");
  const { status, body } = await api("api/save", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ data: state.data, mtime: state.mtime }),
  });
  if (status === 409) { setStatus("changed on disk — reload to keep your edits safe", "conflict"); return; }
  if (status === 400) {
    setStatus("invalid: " + ((body.errors || [body.error]).join("; ")), "error");
    return;
  }
  if (status !== 200) { setStatus("save failed: " + (body && body.error || status), "error"); return; }
  state.mtime = body.mtime;
  state.dirty = false;
  $("saveBtn").disabled = true;
  setStatus("saved", "ok");
  // a save does NOT clear the undo history; it just becomes the new "clean" point
  state.savedJson = snapshot();
  history.lastJson = state.savedJson;
  updateHistoryButtons();
  const cfg = await api("api/config");
  if (cfg.status === 200) {
    state.yaml = cfg.body.yaml; state.errors = cfg.body.errors || [];
    state.warnings = cfg.body.warnings || []; state.isExample = !!cfg.body.is_example;
    setStatus("saved", "ok");
  }
});

$("reloadBtn").addEventListener("click", () => { state.dirty = false; $("saveBtn").disabled = true; load(); });

// ---------------------------------------------------------------- YAML view

$("yamlToggle").addEventListener("click", async () => {
  $("editorView").classList.add("hidden");
  $("yamlView").classList.remove("hidden");
  if (!state.yaml) { const { body } = await api("api/config"); state.yaml = body.yaml || ""; state.mtime = body.mtime; }
  $("yamlText").value = state.yaml;
  $("yamlErrors").innerHTML = "";
  $("yamlStatus").textContent = "";
});
$("yamlBack").addEventListener("click", () => {
  $("yamlView").classList.add("hidden");
  $("editorView").classList.remove("hidden");
});
$("yamlSave").addEventListener("click", async () => {
  const text = $("yamlText").value;
  $("yamlErrors").innerHTML = "";
  $("yamlStatus").textContent = "saving…";
  const { status, body } = await api("api/save_yaml", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ yaml: text, mtime: state.mtime }),
  });
  if (status === 409) { $("yamlStatus").textContent = "changed on disk — reload first"; return; }
  if (status === 400) {
    $("yamlStatus").textContent = "not saved";
    (body.errors || [body.error]).forEach((e) => {
      const li = document.createElement("li"); li.textContent = e; $("yamlErrors").appendChild(li);
    });
    return;
  }
  if (status !== 200) { $("yamlStatus").textContent = "save failed: " + (body && body.error || status); return; }
  state.mtime = body.mtime;
  state.yaml = text;
  state.dirty = false;
  $("saveBtn").disabled = true;
  $("yamlStatus").textContent = "saved";
  await load();          // load() resets the undo history, like after Reload/restore
  $("yamlView").classList.add("hidden");
  $("editorView").classList.remove("hidden");
});

// ---------------------------------------------------------------- save history modal

const histModal = { item: null };   // the history item currently being viewed

function fmtWhen(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return iso || "";
  // browser locale, e.g. "12 Nov, 21:42:05"
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const p = (n) => String(n).padStart(2, "0");
  return d.getDate() + " " + months[d.getMonth()] + ", " + p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
}

function fmtSize(n) {
  if (n == null) return "";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / 1024 / 1024).toFixed(1) + " MB";
}

function openHistory() {
  histModal.item = null;
  $("historyListView").classList.remove("hidden");
  $("historyYamlView").classList.add("hidden");
  $("historyErrors").innerHTML = "";
  $("historyYamlErrors").innerHTML = "";
  $("historyModal").classList.remove("hidden");
  drawHistoryList();
}

function closeHistory() {
  $("historyModal").classList.add("hidden");
  histModal.item = null;
}

async function drawHistoryList() {
  const ul = $("historyList");
  ul.innerHTML = "";
  $("historyEmpty").classList.add("hidden");
  $("historyErrors").innerHTML = "";
  const { status, body } = await api("api/history");
  if (status !== 200 || !body || !Array.isArray(body.items)) {
    $("historyErrors").innerHTML = "";
    const li = document.createElement("li");
    li.textContent = "could not load history: " + ((body && body.error) || status);
    $("historyErrors").appendChild(li);
    return;
  }
  const items = body.items;   // newest first
  if (!items.length) { $("historyEmpty").classList.remove("hidden"); return; }
  items.forEach((it) => {
    const li = document.createElement("li");
    li.className = "history-item";
    const when = document.createElement("span");
    when.className = "history-when";
    when.textContent = fmtWhen(it.saved_at) + "  ·  " + fmtSize(it.size);
    const view = mkBtn("View", () => viewHistoryItem(it));
    const restore = mkBtn("Restore", () => restoreHistoryItem(it));
    restore.classList.add("primary");
    li.append(when, view, restore);
    ul.appendChild(li);
  });
}

async function viewHistoryItem(it) {
  histModal.item = it;
  $("historyListView").classList.add("hidden");
  $("historyYamlView").classList.remove("hidden");
  $("historyYamlErrors").innerHTML = "";
  $("historyYaml").textContent = "loading…";
  const { status, body } = await api("api/history/" + encodeURIComponent(it.id));
  if (status !== 200 || !body) {
    $("historyYaml").textContent = "could not load this version: " + ((body && body.error) || status);
    return;
  }
  $("historyYaml").textContent = body.yaml || "";
}

$("historyBack").addEventListener("click", () => {
  $("historyYamlView").classList.add("hidden");
  $("historyListView").classList.remove("hidden");
  histModal.item = null;
});

async function restoreHistoryItem(it) {
  if (state.dirty && !confirm("Discard your unsaved changes and restore this version?")) return;
  $("historyErrors").innerHTML = "";
  $("historyYamlErrors").innerHTML = "";
  const { status, body } = await api("api/history/" + encodeURIComponent(it.id) + "/restore", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mtime: state.mtime }),
  });
  if (status === 200 && body && body.ok) {
    closeHistory();
    await load();          // resets the undo history, like Reload
    setStatus("restored version from " + fmtWhen(it.saved_at), "ok");
    return;
  }
  if (status === 409) {
    closeHistory();
    setStatus("changed on disk — reload to keep your edits safe", "conflict");
    return;
  }
  // 400 (or anything else): show the errors in the modal
  const errs = (body && (body.errors || [body.error])) || ["restore failed: " + status];
  const target = $("historyYamlView").classList.contains("hidden") ? $("historyErrors") : $("historyYamlErrors");
  errs.forEach((e) => {
    const li = document.createElement("li"); li.textContent = e; target.appendChild(li);
  });
}

$("historyBtn").addEventListener("click", openHistory);
$("historyClose").addEventListener("click", closeHistory);
$("historyRestoreYaml").addEventListener("click", () => { if (histModal.item) restoreHistoryItem(histModal.item); });
$("historyModal").addEventListener("click", (e) => { if (e.target === $("historyModal")) closeHistory(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("historyModal").classList.contains("hidden")) closeHistory();
});

// ---------------------------------------------------------------- boot

bindScreenSettings();
load();

// ---------------------------------------------------------------- clocks (several) + status chip

async function fetchClocks() {
  const { status, body } = await api("api/clocks");
  state.clocks = (status === 200 && body && Array.isArray(body.clocks)) ? body.clocks : [];
  renderClockSelect();
  renderRotation();
  renderMirrorBtn();          // the "Show on clock" button is hidden with no clocks
  const chip = $("clockChip");
  chip.classList.toggle("hidden", state.clocks.length === 0);
  if (state.clocks.length) refreshClockChip();
  refreshAutoRotate();
}

// the toggle's state for the chosen clock ("All clocks" reads the default one)
async function refreshAutoRotate() {
  const q = state.selectedClock ? "?clock=" + encodeURIComponent(state.selectedClock) : "";
  const { body } = await api("api/rotation" + q);
  if (body) $("autoRotate").checked = !!body.enabled;
}

// "main: claude · live · 50%" per clock: current screen (or paused/message/quiet/offline),
// "live" when status.live, brightness as % of 255 when not null.
function chipText(st) {
  if (st.online === false) return "offline";
  if (st.paused) return "paused";
  if (st.message) return String(st.message);
  if (st.quiet) return "quiet";
  const cur = st.current == null ? "—" : String(st.current);
  const live = st.live ? " · live" : "";
  const bri = st.brightness == null ? "" : " · " + Math.round(st.brightness / 255 * 100) + "%";
  return cur + live + bri;
}

async function refreshClockChip() {
  const { status, body } = await api("api/status");
  if (status !== 200 || !body || !body.clocks) return;
  const parts = state.clocks
    .map((c) => c.name + ": " + chipText(body.clocks[c.name] || {}))
    .filter((p) => p);
  $("clockChip").textContent = parts.join("  |  ");
}

setInterval(() => { if (state.clocks.length) refreshClockChip(); }, 15000);

// ---------------------------------------------------------------- automatic cycling on/off
(async function initAutoRotate() {
  const cb = $("autoRotate");
  cb.addEventListener("change", async () => {
    // with 2+ clocks and "All clocks" chosen, set every clock; otherwise the chosen one
    const names = state.clocks.length >= 2 && !state.selectedClock
      ? state.clocks.map((c) => c.name) : [state.selectedClock || null];
    let ok = true, last = null;
    for (const name of names) {
      const { status, body: b } = await api("api/rotation", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(name ? { enabled: cb.checked, clock: name } : { enabled: cb.checked }),
      });
      if (status !== 200 || !b) { ok = false; } else { last = b.enabled; }
    }
    if (!ok) { cb.checked = !cb.checked; setStatus("couldn't change cycling", "error"); return; }
    cb.checked = !!last;
    setStatus(cb.checked ? "cycling automatically" : "button only: press the clock's button for the next screen");
  });
  // when the clock select changes, the toggle shows that clock's state
  $("clockSelect").addEventListener("change", refreshAutoRotate);
})();

fetchClocks();

// ---------------------------------------------------------------- entity name autocomplete
// In any text box of the editor: type a domain and a dot ("sensor.") inside a template, or
// just a few letters in an entity field, and matching entities are offered. Up/Down, Enter or
// Tab picks one, Esc closes.
(function entityAutocomplete() {
  const pop = document.createElement("div");
  pop.className = "ac-pop hidden";
  document.body.appendChild(pop);
  let target = null, start = 0, end = 0, sel = 0, timer = null, seq = 0;
  const wanted = (el) => el && (el.tagName === "TEXTAREA" || (el.tagName === "INPUT" && el.type === "text"))
    && !el.hasAttribute("list") && el.id !== "yamlText" && !(el.placeholder || "").startsWith("search entity");
  const close = () => { pop.classList.add("hidden"); target = null; };
  function mark() { [...pop.children].forEach((c, i) => c.classList.toggle("active", i === sel)); }
  function pick(id) {
    const el = target; if (!el) return;
    el.value = el.value.slice(0, start) + id + el.value.slice(end);
    el.selectionStart = el.selectionEnd = start + id.length;
    close();
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.focus();
  }
  async function look(el) {
    const caret = el.selectionStart == null ? el.value.length : el.selectionStart;
    const before = el.value.slice(0, caret);
    const m = el.dataset.entity ? before.match(/[a-z0-9_.]{2,}$/) : before.match(/[a-z_]{2,}\.[a-z0-9_]*$/);
    if (!m) { close(); return; }
    const mine = ++seq;
    const { status, body } = await api("api/entities?q=" + encodeURIComponent(m[0]));
    if (mine !== seq || status !== 200 || !Array.isArray(body)) return;
    const items = body.filter((e) => e.entity_id !== m[0]).slice(0, 12);
    if (!items.length || document.activeElement !== el) { close(); return; }
    target = el; start = caret - m[0].length; end = caret; sel = 0;
    pop.innerHTML = "";
    items.forEach((e) => {
      const b = document.createElement("div");
      b.className = "ac-opt";
      b.textContent = e.entity_id + (e.state != null ? "  —  " + e.state + (e.unit || "") : "");
      b.dataset.id = e.entity_id;
      b.addEventListener("mousedown", (ev) => { ev.preventDefault(); pick(e.entity_id); });
      pop.appendChild(b);
    });
    const r = el.getBoundingClientRect();
    pop.style.left = (r.left + window.scrollX) + "px";
    pop.style.top = (r.bottom + window.scrollY + 2) + "px";
    pop.style.width = Math.max(240, r.width) + "px";
    pop.classList.remove("hidden");
    mark();
  }
  document.addEventListener("input", (e) => {
    if (!wanted(e.target) || !e.isTrusted) return;
    clearTimeout(timer); timer = setTimeout(() => look(e.target), 200);
  });
  document.addEventListener("keydown", (e) => {
    if (!target || pop.classList.contains("hidden")) return;
    const n = pop.children.length;
    if (e.key === "ArrowDown") { sel = (sel + 1) % n; mark(); }
    else if (e.key === "ArrowUp") { sel = (sel + n - 1) % n; mark(); }
    else if (e.key === "Enter" || e.key === "Tab") pick(pop.children[sel].dataset.id);
    else if (e.key === "Escape") close();
    else return;
    e.preventDefault();
  }, true);
  document.addEventListener("focusout", () => setTimeout(() => { if (document.activeElement !== target) close(); }, 150));
})();
