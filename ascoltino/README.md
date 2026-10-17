# Ascoltino (audio-file patch)

![Supports aarch64 Architecture][aarch64-shield]
![Supports amd64 Architecture][amd64-shield]

A Telegram bot that transcribes voice notes on your own machine and replies
with the text. Nothing is sent to a cloud speech service.

This is [soldatigabriele/ascoltino](https://github.com/soldatigabriele/ascoltino)
1.4.1 with one change: it also transcribes **audio files**. Upstream only reads
Telegram's `voice` field, so a voice note re-shared from WhatsApp, a recorder
app or the Files app - which Telegram delivers as an `audio` file - is dropped
without a reply.

See [DOCS.md](DOCS.md) for setup.

[aarch64-shield]: https://img.shields.io/badge/aarch64-yes-green.svg
[amd64-shield]: https://img.shields.io/badge/amd64-yes-green.svg
