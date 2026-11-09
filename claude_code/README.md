# Claude Code (HA Server)

![Claude Code (HA Server) logo](https://raw.githubusercontent.com/nict41/nict41-s-HA-Apps/main/claude_code/logo.png)

![Supports aarch64 Architecture][aarch64-shield]
![Supports amd64 Architecture][amd64-shield]

[Claude Code](https://docs.anthropic.com/en/docs/claude-code) in a terminal
in your Home Assistant sidebar, with the Home Assistant MCP server and a
privileged-action safety guard. On top of that, this copy runs Claude Code's
**Remote Control server**, so the box shows as a device called **HA Server**
in the Claude mobile app and on claude.ai/code. You can then start new
sessions on it from your phone, several at once, without opening the
Home Assistant panel first.

## Based on hass-claude

This is the Claude Code app from
[sproft/hass-claude](https://github.com/sproft/hass-claude), version 1.2.84
(commit `8126557`), by Robson Felix and that project's contributors, under
the MIT licence (kept in [LICENSE](LICENSE)). Nearly all of the app is their
work: the image, the web terminal, the MCP setup, the privileged-action
guard and the user guide in [DOCS.md](DOCS.md).

### What this copy changes

| Change | Where |
|---|---|
| **Remote Control server.** New options `remote_control_server` (on by default), `remote_control_device_name`, `remote_control_spawn`, `remote_control_capacity` and `remote_control_permission_mode`. At boot the app starts `claude remote-control` in the working directory, in a detached tmux session named `remote-control`, and restarts it with a back-off if it exits. It waits for a Claude login rather than failing, and waits for the boot-time Claude Code update to finish so no session is spawned from a half-written binary. `bypassPermissions` is never passed to it. | [`ha-server-remote-control.sh`](rootfs/usr/local/bin/ha-server-remote-control.sh), Dockerfile startup command |
| **Device name.** Claude Code names a device after its hostname, which the Supervisor fixes inside an app. With `remote_control_hostname_override` on (default), a small `LD_PRELOAD` library, loaded into the Remote Control server process only, reports `remote_control_hostname`, or `remote_control_device_name` (default `HA Server`) when that is empty, as the hostname. At load time it removes itself from `LD_PRELOAD` and drops `HA_SERVER_HOSTNAME`, so sessions the server spawns see the real hostname. | [`hostname_override.c`](rootfs/usr/src/ha-server/hostname_override.c), built in the Dockerfile; AppArmor allows mapping it |
| **Clean stop.** The startup command runs ttyd in the background instead of `exec`, and on SIGTERM first runs `ha-server-remote-control.sh stop`: it sends the Remote Control server SIGTERM and waits up to 45 s, so the server releases its sessions and they reconnect after the restart. `timeout: 60` in `config.yaml` gives it the time. | [`ha-server-remote-control.sh`](rootfs/usr/local/bin/ha-server-remote-control.sh), Dockerfile startup command, `config.yaml` |
| **One-time data import.** On first start, anything staged in `/share/claude_code_import` (marked complete with `.stage-complete`) is copied into this app's private `/data`, before the Claude CLI seed step, and the staging folder is deleted. This moves the CLI, tools and logins kept in `/data` from the original app. | [`ha-server-import-data.sh`](rootfs/usr/local/bin/ha-server-import-data.sh), Dockerfile startup command |
| **Identity.** Name `Claude Code (HA Server)`, slug `claude_code`, version `1.2.84-2`, AppArmor profile `claude_code`, image labels pointing here. Upstream's README became [DOCS.md](DOCS.md), with the new sections added and the install and support links changed. | `config.yaml`, `apparmor.txt`, `Dockerfile`, `DOCS.md` |
| **Tests.** A pytest suite for the two new scripts, and checks that the options, translations and startup command agree. | [`app/`](app) |

Nothing else in upstream's files is changed: the guard scripts, the
`CLAUDE.md` the app writes, `.bashrc`, `.tmux.conf`, the build steps and the
rest of the startup command are as upstream ships them. Pulling in a newer
upstream version means re-applying the rows above.

### Why

Upstream's app starts an interactive `claude` in the web terminal, and only
once someone opens the panel. Turning on Remote Control in that session
shares just that one conversation. Claude Code's server mode,
`claude remote-control`, registers the machine itself, so new sessions can be
started on it from the Claude app. It needs to start with the app to be
useful. This was proposed upstream as a feature request first.

## Using it

Install it from this repository, sign in once in the web terminal (type `c`),
and restart the app. **HA Server** then appears under devices in the Claude
app. The full guide, including how to move from the original app without
losing anything, is in [DOCS.md](DOCS.md).

Don't run this and the original Claude Code app at the same time: they share
`/homeassistant/.claudecode`.

[aarch64-shield]: https://img.shields.io/badge/aarch64-yes-green.svg
[amd64-shield]: https://img.shields.io/badge/amd64-yes-green.svg
