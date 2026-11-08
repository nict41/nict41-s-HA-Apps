/* Tube Display screen editor - plain JS, no build step, relative URLs only
   (the page is served under the HA ingress X-Ingress-Path prefix). */
"use strict";

const WIDGET_TYPES = ["text", "big_number", "ring", "bar", "icon", "image", "sparkline", "blank"];
// fields per widget type, mirroring render._WIDGET_TEMPLATE_FIELDS plus the
// other keys screens.py / render.py read
const FIELDS = {
  text:       ["text", "size", "align", "weight", "color"],
  big_number: ["value", "unit", "label", "unit_newline", "color"],
  ring:       ["value", "max", "label", "unit", "unit_newline", "color"],
  bar:        ["value", "max", "label", "unit", "color"],
  icon:       ["icon", "text", "color"],
  image:      ["source", "fit"],
  sparkline:  ["entity", "hours", "unit", "color"],
  blank:      [],
};
const VALUE_FIELDS = ["value", "text"];   // get the Entity/Fixed/Template source picker
const COLOR_FIELDS = ["color"];

const state = {
  data: null,          // parsed screens file
  yaml: "",
  mtime: null,
  errors: [],
  selectedScreen: null,
  selectedTube: null,
  clipboard: null,
  dirty: false,
  previewTimer: null,
  previewUrl: null,
};

const $ = (id) => document.getElementById(id);

async function api(path, opts) {
  const res = await fetch(path, opts);
  let body = null;
  try { body = await res.json(); } catch (e) { /* png etc. */ }
  return { status: res.status, body, res };
}

function setStatus(text, kind) {
  const el = $("status");
  el.textContent = text || "";
  el.className = "status" + (kind ? " " + kind : "");
  $("reloadBtn").classList.toggle("hidden", kind !== "conflict");
}

function markDirty() {
  state.dirty = true;
  $("saveBtn").disabled = false;
  setStatus("unsaved changes", "dirty");
}

// ---------------------------------------------------------------- load

async function load() {
  const { status, body } = await api("api/config");
  if (status !== 200) { setStatus("could not load config: " + (body && body.error || status), "error"); return; }
  state.yaml = body.yaml;
  state.data = body.data || {};
  state.mtime = body.mtime;
  state.errors = body.errors || [];
  if (!state.data.screens) state.data.screens = {};
  if (!state.data.rotation) state.data.rotation = Object.keys(state.data.screens);
  if (!state.selectedScreen || !state.data.screens[state.selectedScreen]) {
    state.selectedScreen = state.data.rotation.find((r) => r !== "clock") || Object.keys(state.data.screens)[0] || null;
  }
  renderAll();
  setStatus(state.errors.length ? state.errors.length + " error(s) in file" : "loaded", state.errors.length ? "error" : "");
  schedulePreview(0);
}

function renderAll() {
  renderRotation();
  renderScreenList();
  renderScreenSettings();
  renderTubeEditor();
}

// ---------------------------------------------------------------- rotation

function renderRotation() {
  const ul = $("rotationList");
  ul.innerHTML = "";
  const rot = state.data.rotation || [];
  rot.forEach((name, i) => {
    const li = document.createElement("li");
    li.className = "rot-item" + (name === state.selectedScreen ? " selected" : "");
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = true; cb.title = "include in rotation";
    cb.addEventListener("change", () => { if (!cb.checked) removeFromRotation(name); markDirty(); });
    const label = document.createElement("span");
    label.textContent = name;
    label.className = "rot-name";
    label.addEventListener("click", () => { if (name !== "clock") selectScreen(name); });
    const up = mkBtn("↑", () => moveRotation(i, -1));
    const down = mkBtn("↓", () => moveRotation(i, 1));
    li.append(cb, label, up, down);
    ul.appendChild(li);
  });
  const clockOn = rot.includes("clock");
  $("clockEnabled").checked = clockOn;
  const clock = state.data.clock || {};
  $("clockDuration").value = clock.duration != null ? clock.duration : "";
}

function mkBtn(text, fn, title) {
  const b = document.createElement("button");
  b.className = "btn small"; b.textContent = text;
  if (title) b.title = title;
  b.addEventListener("click", fn);
  return b;
}

function moveRotation(i, dir) {
  const rot = state.data.rotation;
  const j = i + dir;
  if (j < 0 || j >= rot.length) return;
  [rot[i], rot[j]] = [rot[j], rot[i]];
  markDirty(); renderRotation();
}

function removeFromRotation(name) {
  state.data.rotation = state.data.rotation.filter((r) => r !== name);
  renderRotation();
}

$("clockEnabled").addEventListener("change", (e) => {
  const rot = state.data.rotation;
  if (e.target.checked && !rot.includes("clock")) rot.push("clock");
  if (!e.target.checked) state.data.rotation = rot.filter((r) => r !== "clock");
  markDirty(); renderRotation();
});
$("clockDuration").addEventListener("change", (e) => {
  const v = parseInt(e.target.value, 10);
  if (v > 0) state.data.clock = { duration: v };
  else delete state.data.clock;
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

$("addScreen").addEventListener("click", () => {
  let name = "screen" + (Object.keys(state.data.screens).length + 1);
  while (state.data.screens[name]) name = "s" + Math.random().toString(36).slice(2, 7);
  state.data.screens[name] = { background: "#000000", tubes: Array.from({ length: 6 }, () => ({ type: "blank" })) };
  if (!state.data.rotation.includes(name)) state.data.rotation.push(name);
  selectScreen(name);
  markDirty();
});

$("renameScreen").addEventListener("click", () => {
  const old = state.selectedScreen;
  if (!old) return;
  const name = prompt("Rename screen", old);
  if (!name || name === old || state.data.screens[name]) return;
  state.data.screens[name] = state.data.screens[old];
  delete state.data.screens[old];
  state.data.rotation = state.data.rotation.map((r) => (r === old ? name : r));
  state.selectedScreen = name;
  markDirty(); renderAll();
});

$("deleteScreen").addEventListener("click", () => {
  const name = state.selectedScreen;
  if (!name || !confirm("Delete screen '" + name + "'?")) return;
  delete state.data.screens[name];
  state.data.rotation = state.data.rotation.filter((r) => r !== name);
  state.selectedScreen = Object.keys(state.data.screens)[0] || null;
  state.selectedTube = null;
  markDirty(); renderAll(); schedulePreview(0);
});

// ---------------------------------------------------------------- screen settings

function renderScreenSettings() {
  const s = state.data.screens[state.selectedScreen];
  $("screenTitle").textContent = state.selectedScreen ? "Screen: " + state.selectedScreen : "No screen selected";
  if (!s) return;
  $("scrDuration").value = s.duration != null ? s.duration : "";
  $("scrRefresh").value = s.refresh != null ? s.refresh : "";
  $("scrBackground").value = /^#[0-9a-fA-F]{6}$/.test(s.background || "") ? s.background : "#000000";
  $("scrWhen").value = s.when || "";
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
}

function curScreen() { return state.data.screens[state.selectedScreen]; }

// ---------------------------------------------------------------- preview

function schedulePreview(ms) {
  clearTimeout(state.previewTimer);
  state.previewTimer = setTimeout(refreshPreview, ms == null ? 600 : ms);
}

async function refreshPreview() {
  const s = curScreen();
  const box = $("tubes");
  if (!s) { box.innerHTML = ""; return; }
  if (state.previewUrl) { URL.revokeObjectURL(state.previewUrl); state.previewUrl = null; }
  const { status, res } = await api("api/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ screen: s }),
  });
  if (status !== 200) { box.innerHTML = '<span class="status error">preview failed</span>'; return; }
  const blob = await res.blob();
  state.previewUrl = URL.createObjectURL(blob);
  box.innerHTML = "";
  for (let i = 0; i < 6; i++) {
    const img = document.createElement("img");
    img.className = "tube-img" + (i === state.selectedTube ? " selected" : "");
    img.src = state.previewUrl;
    img.style.objectPosition = (-i * 135) + "px 0";
    img.title = "tube " + i;
    img.addEventListener("click", () => { state.selectedTube = i; renderTubeEditor(); markSelectedTube(); });
    box.appendChild(img);
  }
}

function markSelectedTube() {
  document.querySelectorAll(".tube-img").forEach((img, i) => {
    img.classList.toggle("selected", i === state.selectedTube);
  });
}

$("refreshPreview").addEventListener("click", () => refreshPreview());

// ---------------------------------------------------------------- tube editor

function renderTubeEditor() {
  const panel = $("tubeEditor");
  const s = curScreen();
  if (!s || state.selectedTube == null || !s.tubes) { panel.classList.add("hidden"); return; }
  panel.classList.remove("hidden");
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
  (FIELDS[type] || []).forEach((f) => box.appendChild(fieldRow(w, f)));
}

function fieldRow(w, f) {
  const row = document.createElement("div");
  row.className = "field-row";
  if (VALUE_FIELDS.includes(f)) {
    row.appendChild(valueSourceEditor(w, f));
  } else if (COLOR_FIELDS.includes(f)) {
    row.appendChild(colorEditor(w, f));
  } else if (f === "unit_newline") {
    const lab = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = !!w[f];
    cb.addEventListener("change", () => { if (cb.checked) w[f] = true; else delete w[f]; markDirty(); schedulePreview(); });
    lab.append(cb, document.createTextNode(" unit on new line"));
    row.appendChild(lab);
  } else if (f === "align") {
    row.appendChild(selectField(w, f, ["center", "left", "right"]));
  } else if (f === "weight") {
    row.appendChild(selectField(w, f, ["", "bold"]));
  } else if (f === "fit") {
    row.appendChild(selectField(w, f, ["cover", "contain"]));
  } else if (f === "size") {
    row.appendChild(selectField(w, f, ["auto", "8", "12", "16", "20", "24", "32", "48", "64", "96", "120"]));
  } else if (f === "max" || f === "hours") {
    row.appendChild(numberField(w, f));
  } else {
    row.appendChild(textField(w, f));
  }
  return row;
}

function labelEl(text) {
  const l = document.createElement("span");
  l.className = "field-label"; l.textContent = text;
  return l;
}

function textField(w, f) {
  const wrap = document.createElement("label");
  wrap.className = "field";
  const input = document.createElement("input");
  input.type = "text"; input.value = w[f] != null ? String(w[f]) : "";
  input.addEventListener("change", () => {
    if (input.value) w[f] = input.value; else delete w[f];
    markDirty(); schedulePreview();
  });
  wrap.append(labelEl(f), input);
  return wrap;
}

function numberField(w, f) {
  const wrap = document.createElement("label");
  wrap.className = "field";
  const input = document.createElement("input");
  input.type = "number"; input.value = w[f] != null ? w[f] : "";
  input.addEventListener("change", () => {
    const v = parseFloat(input.value);
    if (!isNaN(v)) w[f] = v; else delete w[f];
    markDirty(); schedulePreview();
  });
  wrap.append(labelEl(f), input);
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
    if (sel.value === options[0] && (f === "align" || f === "weight" || f === "fit" || f === "size")) delete w[f];
    else if (f === "size" && sel.value !== "auto") w[f] = parseInt(sel.value, 10);
    else w[f] = sel.value;
    markDirty(); schedulePreview();
  });
  wrap.append(labelEl(f), sel);
  return wrap;
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
      const input = document.createElement("input");
      input.type = "text"; input.value = current;
      input.addEventListener("change", () => apply(input.value));
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
  else if (/^#[0-9a-fA-F]{3,6}$/.test(current)) mode = "fixed";

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
      input.value = /^#[0-9a-fA-F]{6}$/.test(current) ? current : "#ffffff";
      input.addEventListener("change", () => apply(input.value));
      const lab = document.createElement("label");
      lab.className = "field";
      lab.append(labelEl(f), input);
      body.appendChild(lab);
    } else if (mode === "thresholds") {
      const ent = m ? m[1] : "";
      const rows = m ? [[m[3], m[2]], [m[5], m[4]]] : [["", "#4CAF50"], ["", "#F44336"]];
      const otherwise = m ? m[6] : "#FFC107";
      const entInput = document.createElement("input");
      entInput.type = "text"; entInput.placeholder = "entity"; entInput.value = ent;
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
      rebuild();
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

  setMode(mode);
  wrap.append(labelEl(f + " source"), tabs, body);
  return wrap;
}

// ---- tube buttons ---------------------------------------------------------

$("tType").addEventListener("change", (e) => {
  const s = curScreen(); if (!s || state.selectedTube == null) return;
  const w = s.tubes[state.selectedTube];
  const old = { ...w };
  const nw = { type: e.target.value };
  // keep fields that exist for the new type
  (FIELDS[e.target.value] || []).forEach((f) => { if (f in old) nw[f] = old[f]; });
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
  const cfg = await api("api/config");
  if (cfg.status === 200) { state.yaml = cfg.body.yaml; state.errors = cfg.body.errors || []; }
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
  await load();
  $("yamlView").classList.add("hidden");
  $("editorView").classList.remove("hidden");
});

// ---------------------------------------------------------------- boot

bindScreenSettings();
load();
