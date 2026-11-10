# Pingvin Share

![Pingvin Share logo](https://raw.githubusercontent.com/nict41/nict41-s-HA-Apps/main/pingvin_share/logo.png)

![Supports aarch64 Architecture][aarch64-shield]
![Supports amd64 Architecture][amd64-shield]

Self-hosted file sharing (a WeTransfer alternative), built on [Pingvin Share X](https://github.com/smp46/pingvin-share-x), the actively maintained fork of the archived Pingvin Share.

## Features

- Self-contained: SQLite database and local file storage, no other apps needed
- A single port, ready to put behind a Cloudflare tunnel
- Optional email, OAuth/LDAP login, S3 storage and ClamAV scanning

## Documentation

Setup, options and troubleshooting are in the app's **Documentation** tab in Home Assistant, or in [DOCS.md](DOCS.md).

[aarch64-shield]: https://img.shields.io/badge/aarch64-yes-green.svg
[amd64-shield]: https://img.shields.io/badge/amd64-yes-green.svg
