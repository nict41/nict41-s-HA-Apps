# nict41's HA Apps

A single Home Assistant **add-on repository** that bundles all of my
previously separate add-ons in one place.

## Add-ons

| Add-on | Folder | Description |
|--------|--------|--------------|
| [3D Print Timelapse](print_timelapse/README.md) | [`print_timelapse/`](print_timelapse) | Captures frame-by-frame 3D print timelapses and archives them as GIFs. |
| [Glance Dashboard](glance/DOCS.md) | [`glance/`](glance) | Self-hosted dashboard for RSS, weather, bookmarks, calendars, stocks, and more. |
| [WordPress](wordpress/DOCS.md) | [`wordpress/`](wordpress) | Self-hosted WordPress, built on the official docker-library/wordpress image. |

Each add-on keeps its own `config.yaml`, `Dockerfile`, and documentation
inside its folder. Originally these lived in separate repos
(`HA3DPrintTimelapse`, `GlanceHA`, `HAWordpress`); they're now maintained
here as one repository.

## Installation

1. In Home Assistant: **Settings → Add-ons → Add-on Store**.
2. Click the **⋮** (overflow) menu in the top right → **Repositories**.
3. Add this repository's URL: `https://github.com/nict41/nict41-s-HA-Apps`.
4. Close the dialog and refresh the page. All three add-ons above will
   appear in the store under "nict41's Home Assistant Add-ons".
5. Click an add-on, then **Install**.

See each add-on's own README/DOCS (linked above) for configuration
options and add-on-specific setup steps.
