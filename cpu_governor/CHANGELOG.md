# Changelog

## 1.0.0

- Initial release. Sets the host CPU cpufreq scaling governor (default
  `performance`) on every start and re-applies it on every host boot via
  `boot: auto` and `startup: system`.
- The add-on first *attempts* to write
  `/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor` directly from its
  own container (it declares `host_pid`, `full_access` and
  `privileged: [SYS_ADMIN, SYS_RAWIO, SYS_RESOURCE, SYS_MODULE]`). On this
  HA install Supervisor mounts `/sys` **read-only** even with all of that
  set, so that write fails and the add-on **falls back** to managing a
  genuinely `--privileged --pid=host` Docker sidecar
  (`cpu-governor-performance`) via the host Docker API (`docker_api: true`),
  which does get a writable `/sys`. See the README for the full privilege
  caveat.
- Idempotent: the sidecar is force-removed and recreated on every start, so
  restarts and reinstalls never leave duplicate containers behind. Because
  the sidecar reuses the same `cpu-governor-performance` name, installing
  this add-on cleanly adopts/replaces any hand-created container of that
  name.
- The chosen governor is configurable via the `governor` option (string,
  default `performance`).
