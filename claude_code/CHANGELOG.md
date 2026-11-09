# Changelog

Versions are upstream's version with a `-N` suffix for this copy's changes.
For changes up to 1.2.84, see
[upstream's changelog](https://github.com/sproft/hass-claude/blob/main/claudecode/CHANGELOG.md).

## 1.2.84-1

- First release: the Claude Code app from sproft/hass-claude 1.2.84, plus:
  - A Remote Control server (`claude remote-control`) started with the app,
    so this box shows as a device in the Claude app and new sessions can be
    started on it from a phone. New options `remote_control_server`,
    `remote_control_device_name`, `remote_control_spawn`,
    `remote_control_capacity` and `remote_control_permission_mode`.
  - The device registers as `remote_control_device_name` (default
    `HA Server`) instead of the container hostname.
  - A one-time import of another Claude Code app's `/data` from
    `/share/claude_code_import`, so moving from the original app keeps the
    CLI, tools and logins stored there.
