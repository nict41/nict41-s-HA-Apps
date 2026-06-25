// Lovelace custom card for the Parcel Tracker add-on.
//
// Reads everything it needs from a single entity's attributes
// (sensor.parcel_tracker_summary, attributes.parcels) via the card's normal
// `hass` property - no network call to the add-on itself, since ingress
// URLs aren't reachable from a card running in the main dashboard frontend.
//
// Install: Settings -> Dashboards -> Resources -> Add resource, URL
// `http://<home-assistant-host>:8000/static/parcel-tracker-card.js`
// (the add-on's direct port, not its ingress URL - ingress paths are
// session-scoped and can't be used as a stable Lovelace resource), type
// JavaScript Module. Then add a card with `type: custom:parcel-tracker-card`.

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

    return `
      <div class="ptc-row">
        <div class="ptc-row-main">
          <div class="ptc-row-title">
            ${trackingLink}
            <span class="ptc-badge" style="background:${color}22;color:${color};">${escapeHtml(label)}</span>
          </div>
          ${sub ? `<div class="ptc-row-sub">${sub}</div>` : ""}
          ${detail ? `<div class="ptc-row-detail">${detail}</div>` : ""}
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
          .ptc-row-main { flex: 1; min-width: 0; }
          .ptc-row-title { display: flex; align-items: center; gap: 8px; font-weight: 500; flex-wrap: wrap; }
          .ptc-row-title a { color: var(--primary-text-color); text-decoration: none; }
          .ptc-row-title a:hover { text-decoration: underline; }
          .ptc-row-sub, .ptc-row-detail { font-size: 0.85em; color: var(--secondary-text-color); margin-top: 2px; }
          .ptc-badge { font-size: 0.72em; font-weight: 600; padding: 1px 7px; border-radius: 999px; white-space: nowrap; }
          .ptc-content { padding-bottom: 4px; }
        </style>
        <div class="ptc-content"></div>
      </ha-card>`;
    this._content = this.querySelector(".ptc-content");
    this._built = true;
  }

  _render() {
    if (!this._built) {
      this._build();
    }

    const parcels = this._parcels();
    if (parcels === null) {
      this._content.innerHTML = `<div class="ptc-empty">Entity "${escapeHtml(this._config.entity)}" not found.</div>`;
      return;
    }

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
