# Changelog

## 0.2.3

- Dependency updates: anyio 4.16.0, pydantic 2.14.3, pydantic-core 2.50.4, sse-starlette 3.7.0, click 8.5.1, typing-inspection 0.4.5.

## 0.2.2

- Security: pillow 13.0.0 (was 12.1.1), which fixes known vulnerabilities.

## 0.2.1
- Documented ESP32 builds in full for other agents: absolute paths, targets, reading errors and warnings, CI=1, artifacts, and that flashing needs another route.

## 0.2.0
- ESP32 firmware builds with PlatformIO: pio_run, pio_artifacts.

## 0.1.1
- Tool failures now return the real reason instead of a bare error.
- The MCP server tells clients how to use it, and DOCS.md covers phones reached over Tailscale.

## 0.1.0
- Initial Android development MCP app.
