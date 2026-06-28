// Lovelace custom card for the Parcel Tracker add-on.
//
// The row list itself reads from a single entity's attributes
// (sensor.parcel_tracker_summary, attributes.parcels) via the card's normal
// `hass` property - that's deliberately kept light (latest status only),
// since it has to fit in a Home Assistant entity attribute. Expanding a
// row's full tracking history needs more than that fits, and the dashboard's
// Archived section isn't synced to hass at all (ha_sync.py deletes the
// entity for any archived/dismissed parcel), so both are fetched on demand
// straight from the add-on's own `/api/parcels` instead, from the same
// origin this script itself was loaded from (see SCRIPT_ORIGIN below) - not
// ingress, which is session-scoped and unusable for a fetch made from
// whatever session happens to be viewing the dashboard right now.
//
// Deliberately read-only: the add-on's write routes (confirm/dismiss/
// archive/delete/reset/etc.) rely entirely on Home Assistant's ingress
// session for access control, so they're intentionally excluded from
// _CardCORSMiddleware in main.py. Porting them here would mean exposing
// unauthenticated mutation over the add-on's direct port to anything on the
// local network that can reach it.
//
// Install: Settings -> Dashboards -> Resources -> Add resource, URL
// `http://<home-assistant-host>:8000/static/parcel-tracker-card.js`
// (the add-on's direct port, not its ingress URL - ingress paths are
// session-scoped and can't be used as a stable Lovelace resource), type
// JavaScript Module. Then add a card with `type: custom:parcel-tracker-card`.

// Resolved once at load time from this script's own <script> tag, not
// `import.meta.url`. Home Assistant's resource loader (loadModule/loadJS)
// always injects a real <script src="..."> element for both "JavaScript
// Module" and "JavaScript File" resource types, so this lookup finds it
// either way - whereas `import.meta` is a parse-time SyntaxError outside a
// module, which would take the whole card down (not just this one origin
// lookup) the moment a resource is registered as "JavaScript File" instead
// of "JavaScript Module" (an easy mix-up in the resource dialog). Falls
// back to the page's own origin only if no matching tag is found at all,
// which would just degrade the on-demand fetches below, not the row list
// itself.
const SCRIPT_ORIGIN = (() => {
  const tag = Array.from(document.getElementsByTagName("script")).find(
    (s) => s.src && s.src.includes("parcel-tracker-card.js")
  );
  return tag ? new URL(tag.src).origin : location.origin;
})();

const escapeHtml = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[c]));

const STATUS_LABELS = {
  pending: "Unconfirmed",
  active: "In transit",
  exception: "Exception",
  delivered: "Delivered",
  archived: "Archived",
  dismissed: "Not a parcel",
};

const STATUS_COLORS = {
  pending: "var(--warning-color, #f2c14e)",
  active: "var(--info-color, #2196f3)",
  exception: "var(--error-color, #db4437)",
  delivered: "var(--success-color, #43a047)",
  archived: "var(--secondary-text-color)",
  dismissed: "var(--secondary-text-color)",
};

// Sections sourced from the lightweight hass attributes. `key` doubles as
// the collapsed-state storage key, so it must stay stable across releases.
const SECTIONS = [
  { key: "pending", title: "Needs confirmation", match: (p) => p.status === "pending" },
  { key: "active", title: "In transit", match: (p) => p.status === "active" || p.status === "exception" },
  { key: "delivered", title: "Delivered", match: (p) => p.status === "delivered" },
];

// Statuses whose courier journey is still meaningful to visualise - archived/
// dismissed parcels are done either way, so they skip the stepper/ETA chip.
const STEPPER_STATUSES = new Set(["pending", "active", "exception", "delivered"]);
const ETA_STATUSES = new Set(["pending", "active", "exception"]);

// Mirrors templates/dashboard.html's stepFor(): binary detected -> out for
// delivery -> delivered, since carrier status text doesn't reliably signal
// any finer-grained step than that.
function stepFor(status, detail) {
  detail = (detail || "").toLowerCase();
  if (status === "delivered") return 4;
  if (/out for delivery|on vehicle|with (the )?courier|loaded for delivery|delivery today/.test(detail)) return 2;
  return 1;
}

// ---- Humanized timestamps --------------------------------------------------
// Mirrors templates/dashboard.html's own formatting helpers so dates read
// the same way in the card as they do on the full dashboard, instead of as
// raw ISO strings.
const RTF = window.Intl && Intl.RelativeTimeFormat
  ? new Intl.RelativeTimeFormat(undefined, { numeric: "auto" })
  : null;

function parseIso(iso) {
  if (!iso) return null;
  // Bare dates ("2026-06-27") are parsed as local midnight rather than UTC,
  // so an estimated-delivery date doesn't slip a day in negative timezones.
  const d = /^\d{4}-\d{2}-\d{2}$/.test(iso) ? new Date(`${iso}T00:00:00`) : new Date(iso);
  return isNaN(d.getTime()) ? null : d;
}

function relativeLabel(d) {
  const secs = Math.round((d.getTime() - Date.now()) / 1000);
  const abs = Math.abs(secs);
  if (abs < 45) return "just now";
  if (!RTF) return d.toLocaleDateString();
  const units = [
    ["year", 31536000], ["month", 2592000], ["week", 604800],
    ["day", 86400], ["hour", 3600], ["minute", 60],
  ];
  for (const [unit, secsPerUnit] of units) {
    if (abs >= secsPerUnit || unit === "minute") return RTF.format(Math.round(secs / secsPerUnit), unit);
  }
  return "just now";
}

function startOfDay(d) {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

function etaLabel(d) {
  const days = Math.round((startOfDay(d) - startOfDay(new Date())) / 86400000);
  if (days === 0) return "Today";
  if (days === 1) return "Tomorrow";
  if (days === -1) return "Yesterday";
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

function carrierChipHtml(parcel) {
  const name = parcel.carrier_name ? escapeHtml(parcel.carrier_name) : "Unknown carrier";
  return `<span class="ptc-chip"><ha-icon icon="mdi:truck-delivery"></ha-icon>${name}</span>`;
}

function etaChipHtml(parcel) {
  if (!ETA_STATUSES.has(parcel.status) || !parcel.estimated_delivery) return "";
  const d = parseIso(parcel.estimated_delivery);
  if (!d) return "";
  return `<span class="ptc-chip" title="Estimated delivery"><ha-icon icon="mdi:calendar"></ha-icon>${escapeHtml(etaLabel(d))}</span>`;
}

function deliveredChipHtml(parcel) {
  if (parcel.status !== "delivered" || !parcel.delivered_at) return "";
  const d = parseIso(parcel.delivered_at);
  if (!d) return "";
  return `<span class="ptc-chip"><ha-icon icon="mdi:check-circle-outline"></ha-icon>Delivered ${escapeHtml(relativeLabel(d))}</span>`;
}

function lastEventChipHtml(parcel) {
  if (parcel.status === "delivered" || !parcel.last_event_time) return "";
  const d = parseIso(parcel.last_event_time);
  if (!d) return "";
  return `<span class="ptc-chip" title="${escapeHtml(d.toLocaleString())}"><ha-icon icon="mdi:clock-outline"></ha-icon>Updated ${escapeHtml(relativeLabel(d))}</span>`;
}

class ParcelTrackerCard extends HTMLElement {
  setConfig(config) {
    this._config = {
      entity: "sensor.parcel_tracker_summary",
      title: "Parcel Tracker",
      ...config,
    };
    this._built = false;
    // Tracking numbers whose history row is currently expanded, kept on the
    // instance (not derived from the DOM) so it survives the full
    // innerHTML rebuild in _render() below.
    this._expanded = new Set();
    this._historyState = "idle"; // idle | loading | loaded | error
    this._historyByTrackingNumber = null;
    // Which section groups (by SECTIONS[].key, plus "archived") the user has
    // collapsed - persisted so a dashboard reload doesn't spring them back
    // open. Scoped to this card's configured entity in case more than one
    // card/entity is in use.
    this._collapsedGroups = this._loadCollapsedGroups();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  getCardSize() {
    const parcels = this._parcels();
    return parcels ? Math.max(1, Math.ceil(parcels.length / 2) + 1) : 1;
  }

  _parcels() {
    const entity = this._hass && this._hass.states[this._config.entity];
    return entity ? entity.attributes.parcels || [] : null;
  }

  _collapseStorageKey() {
    return `ptc-card-collapsed:${this._config.entity}`;
  }

  _loadCollapsedGroups() {
    try {
      return new Set(JSON.parse(localStorage.getItem(this._collapseStorageKey())) || []);
    } catch (e) {
      return new Set();
    }
  }

  _saveCollapsedGroups() {
    try {
      localStorage.setItem(this._collapseStorageKey(), JSON.stringify([...this._collapsedGroups]));
    } catch (e) {}
  }

  // The lightweight `parcel` (from hass attributes) always has
  // tracking_url, since ha_sync.py computes that for every synced parcel.
  // Archived rows instead pass the full /api/parcels record straight in (see
  // _archivedGroupHtml), which has no tracking_url - the link just degrades
  // to plain text in that case.
  _historyHtml(parcel) {
    if (this._historyState === "error") {
      return `<div class="ptc-history-empty">Couldn't load tracking history from the add-on.</div>`;
    }
    if (this._historyState !== "loaded") {
      return `<div class="ptc-history-empty">Loading tracking history&hellip;</div>`;
    }

    const full = this._historyByTrackingNumber[parcel.tracking_number];
    const events = (full && full.tracking_history) || [];

    const metaParts = [
      parcel.tracking_url
        ? `<a href="${escapeHtml(parcel.tracking_url)}" target="_blank" rel="noopener noreferrer">Open carrier tracker</a>`
        : null,
      full && full.tracking_provider ? `Tracked via ${escapeHtml(full.tracking_provider)}` : null,
      full && full.created_at ? `First detected ${escapeHtml(full.created_at)}` : null,
      full && full.updated_at ? `Last checked ${escapeHtml(full.updated_at)}` : null,
    ].filter(Boolean);
    const metaHtml = metaParts.length ? `<div class="ptc-history-meta">${metaParts.join(" &middot; ")}</div>` : "";

    const timelineHtml = events.length
      ? `<ol class="ptc-history-timeline">${events
          .map(
            (event) => `<li>
              <div class="ptc-history-time">${escapeHtml(event.time || "Unknown time")}</div>
              <div class="ptc-history-detail">${escapeHtml(event.detail || "")}${
                event.location ? ` &middot; ${escapeHtml(event.location)}` : ""
              }</div>
            </li>`
          )
          .join("")}</ol>`
      : `<div class="ptc-history-empty">No tracking history available yet.</div>`;

    return metaHtml + timelineHtml;
  }

  _stepperHtml(parcel) {
    if (!STEPPER_STATUSES.has(parcel.status)) return "";
    const idx = stepFor(parcel.status, parcel.status_detail);
    const isException = parcel.status === "exception";
    let dots = "";
    for (let stepNum = 1; stepNum <= 4; stepNum++) {
      dots += `<span class="ptc-step${stepNum <= idx ? " ptc-step-done" : ""}"></span>`;
      if (stepNum < 4) dots += `<span class="ptc-step-bar${stepNum < idx ? " ptc-step-bar-done" : ""}"></span>`;
    }
    return `<div class="ptc-stepper${isException ? " ptc-stepper-exception" : ""}">${dots}</div>`;
  }

  _confidenceHtml(parcel) {
    if (parcel.status !== "pending" || typeof parcel.confidence !== "number") return "";
    const pct = Math.round(parcel.confidence * 100);
    return `<span class="ptc-confidence" title="Detection confidence">
      <span class="ptc-confidence-track"><span class="ptc-confidence-fill" style="width:${pct}%"></span></span>${pct}%
    </span>`;
  }

  _rowHtml(parcel) {
    const status = parcel.status || "";
    const label = STATUS_LABELS[status] || status;
    const color = STATUS_COLORS[status] || "var(--secondary-text-color)";
    const trackingLabel = escapeHtml(parcel.tracking_number);
    const trackingLink = parcel.tracking_url
      ? `<a href="${escapeHtml(parcel.tracking_url)}" target="_blank" rel="noopener noreferrer">${trackingLabel}</a>`
      : trackingLabel;
    const isOpen = this._expanded.has(parcel.tracking_number);

    const subParts = [
      carrierChipHtml(parcel),
      parcel.description ? `<span class="ptc-desc">${escapeHtml(parcel.description)}</span>` : "",
      this._confidenceHtml(parcel),
    ].filter(Boolean).join("");

    const detailParts = [
      parcel.status_detail ? `<span class="ptc-status-detail">${escapeHtml(parcel.status_detail)}</span>` : "",
      etaChipHtml(parcel),
      deliveredChipHtml(parcel),
      lastEventChipHtml(parcel),
    ].filter(Boolean).join("");

    return `
      <div class="ptc-row" data-tracking-number="${escapeHtml(parcel.tracking_number)}">
        <div class="ptc-row-main">
          <div class="ptc-row-title">
            ${trackingLink}
            <span class="ptc-badge" style="background:${color}22;color:${color};">${escapeHtml(label)}</span>
            <span class="ptc-chevron${isOpen ? " ptc-chevron-open" : ""}" aria-hidden="true">&#9662;</span>
          </div>
          ${subParts ? `<div class="ptc-row-sub">${subParts}</div>` : ""}
          ${detailParts ? `<div class="ptc-row-detail">${detailParts}</div>` : ""}
          ${this._stepperHtml(parcel)}
          <div class="ptc-history"${isOpen ? "" : " hidden"}>${isOpen ? this._historyHtml(parcel) : ""}</div>
        </div>
      </div>`;
  }

  _groupHtml(key, title, rowsHtml, count) {
    const collapsed = this._collapsedGroups.has(key);
    return `<div class="ptc-group${collapsed ? " ptc-group-collapsed" : ""}">
      <div class="ptc-group-title" data-group-key="${key}" role="button" tabindex="0" aria-expanded="${!collapsed}">
        <ha-icon class="ptc-group-chevron" icon="mdi:chevron-down"></ha-icon>
        <span>${escapeHtml(title)} (${count})</span>
      </div>
      <div class="ptc-group-body">${rowsHtml}</div>
    </div>`;
  }

  // Archived/dismissed parcels never get an HA entity (ha_sync.py deletes
  // them on purpose), so this group is built entirely from the on-demand
  // /api/parcels fetch rather than from hass attributes - it only appears
  // once that fetch has resolved.
  _archivedGroupHtml() {
    if (this._historyState !== "loaded" || !this._historyByTrackingNumber) return "";
    const items = Object.values(this._historyByTrackingNumber)
      .filter((p) => p.status === "archived" || p.status === "dismissed")
      .sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""));
    if (!items.length) return "";
    return this._groupHtml("archived", "Archived", items.map((p) => this._rowHtml(p)).join(""), items.length);
  }

  _build() {
    this.innerHTML = `
      <ha-card header="${escapeHtml(this._config.title)}">
        <style>
          .ptc-empty { padding: 0 16px 16px; color: var(--secondary-text-color); }
          .ptc-group { padding: 0 16px; }
          .ptc-group-title {
            display: flex; align-items: center; gap: 4px; cursor: pointer; user-select: none;
            font-size: 0.8em; font-weight: 500; color: var(--secondary-text-color);
            margin: 12px 0 4px; text-transform: uppercase; letter-spacing: 0.03em;
          }
          .ptc-group-title:hover { color: var(--primary-text-color); }
          .ptc-group-title:focus-visible { outline: 2px solid var(--primary-color, #03a9f4); outline-offset: 2px; border-radius: 4px; }
          .ptc-group-chevron { --mdc-icon-size: 16px; color: var(--secondary-text-color); transition: transform 0.15s ease; }
          .ptc-group-collapsed .ptc-group-chevron { transform: rotate(-90deg); }
          .ptc-group-collapsed .ptc-group-body { display: none; }
          .ptc-row { display: flex; padding: 8px 0; border-bottom: 1px solid var(--divider-color); }
          .ptc-row:last-child { border-bottom: none; }
          .ptc-row-main { flex: 1; min-width: 0; cursor: pointer; border-radius: 6px; }
          .ptc-row-main:hover { background: var(--secondary-background-color); }
          .ptc-row-title { display: flex; align-items: center; gap: 8px; font-weight: 500; flex-wrap: wrap; }
          .ptc-row-title a { color: var(--primary-text-color); text-decoration: none; }
          .ptc-row-title a:hover { text-decoration: underline; }
          .ptc-row-sub, .ptc-row-detail {
            display: flex; align-items: center; flex-wrap: wrap; gap: 8px;
            font-size: 0.85em; color: var(--secondary-text-color); margin-top: 4px;
          }
          .ptc-desc, .ptc-status-detail { color: var(--secondary-text-color); }
          .ptc-chip { display: inline-flex; align-items: center; gap: 3px; white-space: nowrap; }
          .ptc-chip ha-icon { --mdc-icon-size: 14px; color: var(--secondary-text-color); }
          .ptc-badge { font-size: 0.72em; font-weight: 600; padding: 1px 7px; border-radius: 999px; white-space: nowrap; }
          .ptc-chevron {
            font-size: 0.65em; color: var(--primary-color, #03a9f4); transition: transform 0.15s ease;
          }
          .ptc-chevron-open { transform: rotate(180deg); }
          .ptc-confidence { display: inline-flex; align-items: center; gap: 4px; white-space: nowrap; }
          .ptc-confidence-track { width: 40px; height: 4px; border-radius: 999px; background: var(--divider-color); overflow: hidden; }
          .ptc-confidence-fill { display: block; height: 100%; background: var(--warning-color, #f2c14e); }
          .ptc-stepper { display: flex; align-items: center; margin-top: 8px; max-width: 200px; }
          .ptc-step { width: 8px; height: 8px; border-radius: 50%; background: var(--divider-color); flex: none; }
          .ptc-step-done { background: var(--primary-color, #03a9f4); }
          .ptc-stepper-exception .ptc-step-done { background: var(--error-color, #db4437); }
          .ptc-step-bar { flex: 1; height: 2px; background: var(--divider-color); margin: 0 3px; }
          .ptc-step-bar-done { background: var(--primary-color, #03a9f4); }
          .ptc-stepper-exception .ptc-step-bar-done { background: var(--error-color, #db4437); }
          .ptc-history { margin-top: 6px; padding-top: 6px; border-top: 1px solid var(--divider-color); }
          .ptc-history-meta {
            display: flex; gap: 8px; flex-wrap: wrap; font-size: 0.78em;
            color: var(--secondary-text-color); margin-bottom: 6px;
          }
          .ptc-history-meta a { color: var(--primary-color, #03a9f4); text-decoration: none; }
          .ptc-history-meta a:hover { text-decoration: underline; }
          .ptc-history-empty { font-size: 0.8em; color: var(--secondary-text-color); }
          .ptc-history-timeline { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
          .ptc-history-timeline li { padding-left: 8px; border-left: 2px solid var(--divider-color); }
          .ptc-history-time { font-size: 0.74em; color: var(--primary-color, #03a9f4); }
          .ptc-history-detail { font-size: 0.82em; color: var(--primary-text-color); margin-top: 2px; }
          .ptc-content { padding-bottom: 4px; }
        </style>
        <div class="ptc-content"></div>
      </ha-card>`;
    this._content = this.querySelector(".ptc-content");
    this._content.addEventListener("click", (e) => this._onContentClick(e));
    this._content.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      const groupTitle = e.target.closest(".ptc-group-title");
      if (!groupTitle) return;
      e.preventDefault();
      this._toggleGroup(groupTitle);
    });
    this._built = true;
  }

  _toggleGroup(groupTitleEl) {
    const key = groupTitleEl.dataset.groupKey;
    const groupEl = groupTitleEl.closest(".ptc-group");
    const wasCollapsed = this._collapsedGroups.has(key);
    if (wasCollapsed) this._collapsedGroups.delete(key);
    else this._collapsedGroups.add(key);
    this._saveCollapsedGroups();
    groupEl.classList.toggle("ptc-group-collapsed", !wasCollapsed);
    groupTitleEl.setAttribute("aria-expanded", String(wasCollapsed));
  }

  // Delegated on the content container (rather than per-row/per-group) so it
  // keeps working after _render() replaces the rows' innerHTML wholesale.
  _onContentClick(e) {
    const groupTitle = e.target.closest(".ptc-group-title");
    if (groupTitle) {
      this._toggleGroup(groupTitle);
      return;
    }

    if (e.target.closest("a")) return; // let tracking/carrier links navigate normally
    const mainEl = e.target.closest(".ptc-row-main");
    if (!mainEl) return;
    const row = mainEl.closest(".ptc-row");
    const trackingNumber = row.dataset.trackingNumber;
    const historyEl = row.querySelector(".ptc-history");
    const chevronEl = row.querySelector(".ptc-chevron");
    const opening = historyEl.hidden;

    if (opening) {
      this._expanded.add(trackingNumber);
    } else {
      this._expanded.delete(trackingNumber);
    }
    historyEl.hidden = !opening;
    chevronEl.classList.toggle("ptc-chevron-open", opening);

    if (opening) {
      const parcel = this._findParcelForRow(trackingNumber);
      if (parcel) historyEl.innerHTML = this._historyHtml(parcel);
      this._ensureHistoryLoaded();
    }
  }

  // Looks a row's parcel up from the lightweight hass-sourced list first
  // (the common case), falling back to the fetched /api/parcels record for
  // rows that only exist there - i.e. the Archived group.
  _findParcelForRow(trackingNumber) {
    const lightweight = (this._parcels() || []).find((p) => p.tracking_number === trackingNumber);
    if (lightweight) return lightweight;
    return this._historyByTrackingNumber && this._historyByTrackingNumber[trackingNumber];
  }

  // Fetches every parcel's full record once and caches it for the life of
  // this card instance - retried on the next expand if it ever fails, but
  // never re-fetched just because the user re-opens a row that's already
  // loaded. Kicked off eagerly (see _render()) rather than only on first
  // expand, since the Archived group depends on it too.
  _ensureHistoryLoaded() {
    if (this._historyState === "loading" || this._historyState === "loaded") return;
    this._historyState = "loading";
    fetch(`${SCRIPT_ORIGIN}/api/parcels`)
      .then((resp) => {
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        return resp.json();
      })
      .then((body) => {
        this._historyByTrackingNumber = {};
        (body.parcels || []).forEach((p) => {
          this._historyByTrackingNumber[p.tracking_number] = p;
        });
        this._historyState = "loaded";
      })
      .catch(() => {
        this._historyState = "error";
      })
      // Forces a rebuild even though the hass-sourced parcels haven't
      // changed, so any already-open history panel and the Archived group
      // both pick up the now-resolved (or failed) fetch.
      .then(() => this._render(true));
  }

  _render(force) {
    if (!this._built) {
      this._build();
    }

    const parcels = this._parcels();
    if (parcels === null) {
      // Cleared so that if the entity reappears later (e.g. after a Home
      // Assistant restart - these states aren't tied to a registered
      // integration, so they don't survive one on their own) with the same
      // parcel data as before it vanished, the key check below doesn't
      // mistake that for "nothing changed" and leave this error showing.
      this._lastParcelsKey = null;
      this._content.innerHTML = `<div class="ptc-empty">Entity "${escapeHtml(this._config.entity)}" not found.</div>`;
      return;
    }

    // Idempotent once loading/loaded - kicked off here (rather than only on
    // first history expand) so the Archived group can appear without the
    // user needing to open anything first.
    this._ensureHistoryLoaded();

    // `hass` is set on every entity state change in the whole house, not
    // just this card's own entity - rebuilding every row's innerHTML on
    // every single one of those would otherwise collapse any row a user
    // has expanded the instant anything else in Home Assistant changes.
    const parcelsKey = JSON.stringify(parcels);
    if (!force && parcelsKey === this._lastParcelsKey) return;
    this._lastParcelsKey = parcelsKey;

    const groupsHtml = SECTIONS.map(({ key, title, match }) => {
      const items = parcels.filter(match);
      if (!items.length) return "";
      return this._groupHtml(key, title, items.map((p) => this._rowHtml(p)).join(""), items.length);
    }).join("") + this._archivedGroupHtml();

    this._content.innerHTML = groupsHtml || `<div class="ptc-empty">No tracked parcels.</div>`;
  }
}

customElements.define("parcel-tracker-card", ParcelTrackerCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "parcel-tracker-card",
  name: "Parcel Tracker Card",
  description: "Shows tracked parcels from the Parcel Tracker add-on.",
});
