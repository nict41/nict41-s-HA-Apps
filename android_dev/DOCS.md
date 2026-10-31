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

This HA host has no `/dev/kvm`, so Android emulators are not supported. Use a real phone over wireless adb.

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
