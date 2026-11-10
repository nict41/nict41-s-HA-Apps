# WordPress

![WordPress logo](https://raw.githubusercontent.com/nict41/nict41-s-HA-Apps/main/wordpress/logo.png)

![Supports aarch64 Architecture][aarch64-shield]
![Supports amd64 Architecture][amd64-shield]
![Supports armhf Architecture][armhf-shield]
![Supports armv7 Architecture][armv7-shield]
![Supports i386 Architecture][i386-shield]

Self-hosted WordPress built on the official [docker-library/wordpress](https://github.com/docker-library/wordpress) image, with a built-in MariaDB server, so it needs no other apps.

## Features

- Works with no configuration: the database and user are created on first start
- Daily compressed database dumps alongside Home Assistant's own backups
- Optional external MySQL/MariaDB, locale, debug mode and extra `wp-config.php` settings

## Documentation

Setup, options and troubleshooting are in the app's **Documentation** tab in Home Assistant, or in [DOCS.md](DOCS.md).

[aarch64-shield]: https://img.shields.io/badge/aarch64-yes-green.svg
[amd64-shield]: https://img.shields.io/badge/amd64-yes-green.svg
[armhf-shield]: https://img.shields.io/badge/armhf-yes-green.svg
[armv7-shield]: https://img.shields.io/badge/armv7-yes-green.svg
[i386-shield]: https://img.shields.io/badge/i386-yes-green.svg
