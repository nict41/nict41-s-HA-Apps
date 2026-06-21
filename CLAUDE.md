# nict41's HA Apps

A Home Assistant app repository (`repository.yaml` + one folder per app).
Each app folder is a standalone Supervisor app: `config.yaml`, `Dockerfile`,
`build.yaml`, `icon.png`/`logo.png`, `CHANGELOG.md`, and a `README.md` or
`DOCS.md`.

## When developing a new app in this repo

Whenever asked to add/develop a new app here, always also:

1. **Give it a nice `icon.png` (128x128, square) and `logo.png` (~250x100 or
   500x100)**, styled consistently with the existing apps: dark navy
   background (`#181823`-ish), flat geometric shapes, one accent color per
   app. Generate with Pillow if no other image tool is available — see
   `print_timelapse`, `glance`, and `wordpress` icons/logos for the
   established style.
2. **Write a professional, self-contained `README.md`/`DOCS.md`** for the
   app — model it on `print_timelapse/README.md`. It should embed the
   app's logo near the top and cover everything a user needs without
   having to dig through source: what the app does, setup/configuration
   steps, full options reference, and any REST/automation examples needed
   to actually use it (not just a link back to this root README).
3. **Update the root `README.md`**: add a new `### [App Name](./folder)`
   section under `## Apps`, including the app's logo image, architecture
   support shields, and a one-line description — following the existing
   per-app sections there.

## Terminology

Home Assistant renamed "add-ons" to "apps". Use "app" in all prose/docs.
Do **not** rename still-current literal/technical identifiers, e.g.
`repository.yaml`, `config.yaml`, the `addon_config` map key, the
`/addon_configs/<slug>` mount path, or the proper noun "Home Assistant
Community Add-ons" (a real third-party repo name).
