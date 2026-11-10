# CPU Performance Governor

![CPU Performance Governor logo](https://raw.githubusercontent.com/nict41/nict41-s-HA-Apps/main/cpu_governor/logo.png)

![Supports aarch64 Architecture][aarch64-shield]
![Supports amd64 Architecture][amd64-shield]

Sets the host CPU's [cpufreq scaling governor](https://www.kernel.org/doc/html/latest/admin-guide/pm/cpufreq.html) (`performance` by default) so CPU-bound jobs get the full clock speed instead of being held back by `powersave`.

## Features

- Applies the governor on start and again on every host boot
- Tries a direct write first and falls back to a short-lived privileged helper container only when the host requires it
- Cleans up after itself, so restarts and reinstalls never leave duplicate containers

> [!NOTE]
> Written for an Intel N100 Home Assistant OS host. It needs Docker API access, so read the documentation before installing.

## Documentation

Setup, options and troubleshooting are in the app's **Documentation** tab in Home Assistant, or in [DOCS.md](DOCS.md).

[aarch64-shield]: https://img.shields.io/badge/aarch64-yes-green.svg
[amd64-shield]: https://img.shields.io/badge/amd64-yes-green.svg
