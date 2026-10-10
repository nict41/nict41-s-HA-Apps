# Changelog

## 0.2.5

- Ask for the ingress path under whichever name the base image's bashio
  provides. bashio 0.18 renamed every `bashio::addon.*` function to
  `bashio::app.*`; the old names still work, generated at load time as
  aliases that warn on stderr and delegate, so nothing is broken today.
  Deprecated is not permanent though, and this one call is how the app
  learns the path Home Assistant serves it from — losing it would not
  crash anything, it would quietly disable the sidebar. Now it prefers
  `bashio::app.ingress_entry` and falls back to the old name.
- No behaviour change on any base image shipping bashio today. The helper
  was exercised against all four cases: both names present (and no
  deprecation warning emitted, since the new name is called directly),
  only the old name, only the new name, and no ingress path at all.

## 0.2.4

- Move to Glance v0.8.6, two patch releases on from v0.8.4.
- Nothing in this add-on changed. The parts it depends on are unchanged
  upstream: the binary is still at `/app/glance`, still takes `--config`,
  and still listens on 8080. The bundled default dashboard and both
  configurations this add-on generates — the ingress one carrying
  `server.base-url` and the direct one without it — were each checked
  against the v0.8.6 binary with its own `config:validate`, so a config
  key moving under you would have shown up here rather than at start.
- Your own `glance/glance.yml` in the Home Assistant config folder is
  untouched, and only its `pages:` section is read, as before.

## 0.2.3

- Fix the add-on showing as "Error" instead of "Stopped" after a normal
  stop. The `glance`, `glance-direct`, and `nginx` service `finish`
  scripts were treating *any* exit — including the expected SIGTERM sent
  when Home Assistant stops the add-on — as a crash, writing exit code
  143 as the container's result and force-halting. They now recognize a
  SIGTERM-caused exit as a normal stop and let s6-overlay's own shutdown
  finish with exit code 0 instead.

## 0.2.2

- Fix direct browser access (port 8099) having broken CSS/widgets
- Run two Glance instances: one with base-url for HA ingress, one without for direct access
- nginx routes to the correct instance using HA's X-Ingress-Path header

## 0.2.1

- Add icon and logo

## 0.2.0

- Move user config to `glance/glance.yml` in the main HA config folder
- Config is now editable directly in File Editor without navigating to addon_configs
- Fix: config file was not accessible/writable via addon_config mount

## 0.1.9

- Log the active config file path on startup
- Log a hint on first install showing how to customise the dashboard

## 0.1.8

- Fix widgets not loading through ingress (SSE/JavaScript URLs broken)
- Switch from nginx sub_filter rewriting to setting Glance's `base-url`
- Glance now generates all URLs (HTML and JavaScript/SSE) with the ingress prefix
- nginx is now a plain passthrough proxy

## 0.1.7

- Fix: `/addon_configs/self` directory not created on startup, causing config copy to fail
- Fix: switched to proper s6-overlay oneshot/longrun services with `with-contenv`
- Fix: `init: false` required for SUPERVISOR_TOKEN to be available in services
- Runtime config written to `/run/glance/glance.yml` to avoid busybox sed issues

## 0.1.0

- Initial release
- Based on Glance v0.8.4
- Home Assistant ingress support
- Multi-arch builds: amd64, aarch64, armv7
- Default dashboard with weather, RSS, bookmarks, clock, and market widgets
