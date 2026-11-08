# Android Dev

Android Dev runs the Android SDK, Java and Gradle in a Debian-based Home Assistant app and exposes build and phone automation tools through MCP. Claude Code can edit source directly under `/share/android_dev/projects/<name>` because that directory is shared with the separate development app.

## Setup

Set `mcp_token` to a random secret of at least 32 characters. Requests to `/mcp` require `Authorization: Bearer <token>`. A missing or short token leaves the app running but disables MCP with HTTP 503. `/health` is unauthenticated for Supervisor watchdog checks.

`accept_android_sdk_license` defaults to false. Android SDK downloads do not start while it is false. Enabling it is your decision to accept Google's Android SDK licence. SDK archives and the Gradle distribution are verified before installation.

Register the server with Claude Code using its HTTP MCP transport and bearer header, for example:

```sh
claude mcp add --transport http android-dev http://beb500c8-android-dev:8200/mcp --header "Authorization: Bearer YOUR_LONG_TOKEN"
```

The app exposes port 8200 to the internal app network; it is not published to the host by default. Do not publish that port. The token allows code execution in this app because Gradle builds run project code, and shell commands on paired phones.

## Wireless phone connection

On the phone, enable Developer options and Wireless debugging. Choose **Pair device with pairing code** and pass the displayed pairing IP and port to `adb_pair` along with its code. Pairing and connect use different ports. Then use the main Wireless debugging screen's IP address and port with `adb_connect`. Android may change these ports, so check the phone each time. If several phones are connected, supply the device id to device-specific tools.

Over Tailscale, the phone must still be on Wi-Fi, because Android only offers Wireless debugging there. Use the phone's Tailscale `100.x` address in place of the IP shown on screen, and keep the port shown.

This HA host has no `/dev/kvm`, so Android emulators are not supported. Use a real phone over wireless adb.

## Typical workflow

Write the project under `/share/android_dev/projects/<name>`, run `gradle_wrapper` once, then start a build with `gradle_run`. It returns a job id straight away, so poll `job_status` and then use `list_artifacts`. Only one job runs at a time. Install with `install_apk`, then use `launch`, `logcat`, `screenshot` and `ui_dump`. `input_text` is ASCII only, and a literal `%s` types a space. AGP 9.5.0, compileSdk 36 and Gradle 9.8.2 are known to work on JDK 21.

## ESP32 builds (PlatformIO)

The app also compiles ESP32 firmware with PlatformIO 6.2.0. Use it from any container that can't run the
xtensa toolchain itself, such as the Alpine-based Claude Code app.

### Where projects can live

`pio_run` and `pio_artifacts` take the project's **absolute path**, not a project name like the Gradle
tools. The path must contain `platformio.ini` and be inside an allowed root:

- `/share/android_dev/projects` (any project under it)
- `/share/elekstube-repo` (the IPSTube clock firmware, fork `nict41/EleksTubeIPS`)

Symlinks and `..` are resolved first, so they can't reach anywhere else. To allow more roots, set the
colon-separated `PIO_PROJECT_ROOTS` environment variable for the app.

### Workflow

1. Edit the source directly under `/share/...` from your own container.
2. `pio_run(path="/share/elekstube-repo", environment="ipstube")` starts the build and returns a
   `job_id` straight away. Leave out `environment` to build every environment in `platformio.ini`,
   which is much slower. Pass PlatformIO targets with `targets`, for example `["buildfs"]` for the
   filesystem image or a project's own custom targets such as `["release"]`.
3. Poll `job_status(job_id, tail_lines=...)` until `state` leaves `running`. The log tail is where
   compiler errors and warnings appear; ask for more lines (for example 400) and search for
   `error:` and `warning:`. `job_cancel` stops a build.
4. `pio_artifacts(path, environment)` lists `firmware.bin`, `firmware.elf`, `partitions.bin`,
   `bootloader.bin`, `littlefs.bin` from `.pio/build/<environment>/`, plus any `releases/*.bin`,
   newest first.

### What the build environment does

- `CI=1` is set. Projects can behave differently under it: the EleksTube firmware then builds its web
  pages with `npm ci --ignore-scripts` and fails the build if that fails, instead of falling back to
  old pages.
- Node.js and npm are installed for projects that build web assets.
- `PLATFORMIO_CORE_DIR=/data/platformio` holds the platforms, toolchains and libraries. The first build
  of a platform downloads about 1 GB there; later builds reuse it. It's excluded from backups.
- Telemetry is off.
- Builds run as root inside this app, so `.pio/` (and `web/node_modules/` for EleksTube) in the project
  are created by root. Both are in the projects' `.gitignore`.
- Gradle and PlatformIO share **one job slot**: a second `pio_run` or `gradle_run` returns an error
  while a job is running. The timeout is the `gradle_job_timeout_minutes` option.

### Limits

- **No flashing or serial monitor.** The app has no USB access to the device, so the `upload` and
  `monitor` targets don't work. Flash the built files another way: over Wi-Fi if the firmware supports
  it, or from a computer with USB (esptool or a web flasher).
- Only `amd64`, like the rest of the app.

## Projects, artifacts and signing

Projects live in `/share/android_dev/projects`; screenshots are written to `/share/android_dev/screenshots`. SDK files live in `/data/android-sdk`, Gradle downloads in `/data/gradle`, the pinned Gradle executable in `/data/tools/gradle-9.8.2`, adb keys in `/data/adb`, build logs in `/data/jobs`, and signing materials in `/data/keystores`.

Create a signing keystore with `keystore_create`. The tool returns its SHA-256 fingerprint, never its generated passwords. To sign with Gradle, provide `signing_keystore` to `gradle_run` and read the injected environment variables in your Android `signingConfigs`:

```kotlin
signingConfigs {
    create("release") {
        System.getenv("ANDROID_SIGNING_STORE_FILE")?.let { storeFile = file(it) }
        storePassword = System.getenv("ANDROID_SIGNING_STORE_PASSWORD")
        keyAlias = System.getenv("ANDROID_SIGNING_KEY_ALIAS")
        keyPassword = System.getenv("ANDROID_SIGNING_KEY_PASSWORD")
    }
}
```

The `?.let` keeps debug builds working when no keystore is supplied.

Keystores are included in app backups and are the only copy held by this app. Losing a release keystore can prevent updates to apps already published with it. Keep a separate secure backup too.

## Resources

The app is intended for the HAOS amd64 Intel N100 host (4 cores, 31 GB RAM). Gradle is limited by default to 6 GB and 3 workers. Tune the app options to leave resources for Home Assistant and other apps.
