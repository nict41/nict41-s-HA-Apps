// Lovelace custom card for the Parcel Tracker add-on.
//
// The row list itself reads from a single entity's attributes
// (sensor.parcel_tracker_summary, attributes.parcels) via the card's normal
// `hass` property - that's deliberately kept light (latest status only),
// since it has to fit in a Home Assistant entity attribute. Expanding a
// row's full tracking history needs more than that fits, so it's fetched on
// demand straight from the add-on's own `/api/parcels` instead, from the
// same origin this script itself was loaded from (see SCRIPT_ORIGIN below) -
// not ingress, which is session-scoped and unusable for a fetch made from
// whatever session happens to be viewing the dashboard right now.
//
// Install: Settings -> Dashboards -> Resources -> Add resource, URL
// `http://<home-assistant-host>:8000/static/parcel-tracker-card.js`
// (the add-on's direct port, not its ingress URL - ingress paths are
// session-scoped and can't be used as a stable Lovelace resource), type
// JavaScript Module. Then add a card with `type: custom:parcel-tracker-card`.

// Resolved once at load time from this script's own URL, since that's
// already known to be reachable (it's how this very file got loaded) -
// the history fetch below reuses that same origin rather than asking the
// user to configure it separately.
const SCRIPT_ORIGIN = new URL(import.meta.url).origin;

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
};

const STATUS_COLORS = {
  pending: "var(--warning-color, #f2c14e)",
  active: "var(--info-color, #2196f3)",
  exception: "var(--error-color, #db4437)",
  delivered: "var(--success-color, #43a047)",
};

const SECTIONS = [
  { title: "Needs confirmation", match: (p) => p.status === "pending" },
  { title: "In transit", match: (p) => p.status === "active" || p.status === "exception" },
  { title: "Delivered", match: (p) => p.status === "delivered" },
];

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

  // The lightweight `parcel` (from hass attributes) always has
  // tracking_url, since ha_sync.py computes that for every synced parcel -
  // the full per-event history and bookkeeping timestamps only exist on
  // the heavier object fetched from /api/parcels on first expand.
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

  _rowHtml(parcel) {
    const status = parcel.status || "";
    const label = STATUS_LABELS[status] || status;
    const color = STATUS_COLORS[status] || "var(--secondary-text-color)";
    const sub = [parcel.description, parcel.carrier_name].filter(Boolean).map(escapeHtml).join(" &middot; ");
    const detail = [
      parcel.status_detail,
      parcel.estimated_delivery ? `est. delivery ${parcel.estimated_delivery}` : null,
    ]
      .filter(Boolean)
      .map(escapeHtml)
      .join(" &middot; ");
    const trackingLabel = escapeHtml(parcel.tracking_number);
    const trackingLink = parcel.tracking_url
      ? `<a href="${escapeHtml(parcel.tracking_url)}" target="_blank" rel="noopener noreferrer">${trackingLabel}</a>`
      : trackingLabel;
    const isOpen = this._expanded.has(parcel.tracking_number);

    return `
      <div class="ptc-row" data-tracking-number="${escapeHtml(parcel.tracking_number)}">
        <div class="ptc-row-main">
          <div class="ptc-row-title">
            ${trackingLink}
            <span class="ptc-badge" style="background:${color}22;color:${color};">${escapeHtml(label)}</span>
            <span class="ptc-chevron${isOpen ? " ptc-chevron-open" : ""}" aria-hidden="true">&#9662;</span>
          </div>
          ${sub ? `<div class="ptc-row-sub">${sub}</div>` : ""}
          ${detail ? `<div class="ptc-row-detail">${detail}</div>` : ""}
          <div class="ptc-history"${isOpen ? "" : " hidden"}>${isOpen ? this._historyHtml(parcel) : ""}</div>
        </div>
      </div>`;
  }

  _build() {
    this.innerHTML = `
      <ha-card header="${escapeHtml(this._config.title)}">
        <style>
          .ptc-empty { padding: 0 16px 16px; color: var(--secondary-text-color); }
          .ptc-group { padding: 0 16px; }
          .ptc-group-title {
            font-size: 0.8em; font-weight: 500; color: var(--secondary-text-color);
            margin: 12px 0 4px; text-transform: uppercase; letter-spacing: 0.03em;
          }
          .ptc-row { display: flex; padding: 8px 0; border-bottom: 1px solid var(--divider-color); }
          .ptc-row:last-child { border-bottom: none; }
          .ptc-row-main { flex: 1; min-width: 0; cursor: pointer; border-radius: 6px; }
          .ptc-row-main:hover { background: var(--secondary-background-color); }
          .ptc-row-title { display: flex; align-items: center; gap: 8px; font-weight: 500; flex-wrap: wrap; }
          .ptc-row-title a { color: var(--primary-text-color); text-decoration: none; }
          .ptc-row-title a:hover { text-decoration: underline; }
          .ptc-row-sub, .ptc-row-detail { font-size: 0.85em; color: var(--secondary-text-color); margin-top: 2px; }
          .ptc-badge { font-size: 0.72em; font-weight: 600; padding: 1px 7px; border-radius: 999px; white-space: nowrap; }
          .ptc-chevron {
            font-size: 0.65em; color: var(--primary-color, #03a9f4); transition: transform 0.15s ease;
          }
          .ptc-chevron-open { transform: rotate(180deg); }
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
    this._built = true;
  }

  // Delegated on the content container (rather than per-row) so it keeps
  // working after _render() replaces the rows' innerHTML wholesale.
  _onContentClick(e) {
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
      const parcel = (this._parcels() || []).find((p) => p.tracking_number === trackingNumber);
      if (parcel) historyEl.innerHTML = this._historyHtml(parcel);
      this._ensureHistoryLoaded();
    }
  }

  // Fetches every parcel's full record once and caches it for the life of
  // this card instance - retried on the next expand if it ever fails, but
  // never re-fetched just because the user re-opens a row that's already
  // loaded.
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
      .then(() => this._repaintExpandedHistory());
  }

  // Updates already-open history panels in place once the fetch above
  // settles, without touching the rest of the row (a full _render() call
  // would be skipped anyway here, since the underlying hass attributes -
  // the only thing it diffs against - haven't changed).
  _repaintExpandedHistory() {
    if (!this._content) return;
    const parcels = this._parcels() || [];
    this._content.querySelectorAll(".ptc-row").forEach((row) => {
      const trackingNumber = row.dataset.trackingNumber;
      if (!this._expanded.has(trackingNumber)) return;
      const parcel = parcels.find((p) => p.tracking_number === trackingNumber);
      const historyEl = row.querySelector(".ptc-history");
      if (parcel && historyEl) historyEl.innerHTML = this._historyHtml(parcel);
    });
  }

  _render() {
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

    // `hass` is set on every entity state change in the whole house, not
    // just this card's own entity - rebuilding every row's innerHTML on
    // every single one of those would otherwise collapse any row a user
    // has expanded the instant anything else in Home Assistant changes.
    const parcelsKey = JSON.stringify(parcels);
    if (parcelsKey === this._lastParcelsKey) return;
    this._lastParcelsKey = parcelsKey;

    const groupsHtml = SECTIONS.map(({ title, match }) => {
      const items = parcels.filter(match);
      if (!items.length) return "";
      return `<div class="ptc-group"><div class="ptc-group-title">${escapeHtml(title)} (${items.length})</div>${items
        .map((p) => this._rowHtml(p))
        .join("")}</div>`;
    }).join("");

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
