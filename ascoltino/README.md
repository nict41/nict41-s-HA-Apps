# Transcribe-a-bot

![Transcribe-a-bot logo](https://raw.githubusercontent.com/nict41/nict41-s-HA-Apps/main/ascoltino/logo.png)

![Supports aarch64 Architecture][aarch64-shield]
![Supports amd64 Architecture][amd64-shield]

A private Telegram bot that turns voice notes into text **on your own
machine**. Forward it a voice note - from a Telegram chat, from WhatsApp, from
a recorder app - and it replies with the transcript a few seconds later. No
audio goes to a cloud speech service.

```
You --forward voice note--> Telegram --> this app (long-polls the Bot API)
                                            |  downloads the audio
                                            |  transcribes locally (Parakeet / Whisper)
You <------- transcript reply <-------------+
```

The app makes outbound connections only: to Telegram, and to Hugging Face
once to download the model. It needs no open port, webhook or tunnel.

## What it transcribes

| Telegram delivers it as | Typical source | Upstream | This app |
|---|---|---|---|
| `voice` | Recorded in Telegram, or forwarded from a Telegram chat | ✅ | ✅ |
| `audio` | Voice note shared from WhatsApp or a recorder app; mp3/m4a files | ❌ silently ignored | ✅ |
| `video_note` | Telegram round video message | ❌ | ✅ |
| `document` with an `audio/*` type | Audio sent "as a file" | ❌ | ✅ |

Telegram only lets bots download files up to **20 MB**, which is tens of
minutes of compressed speech.

## Based on Ascoltino

This is [soldatigabriele/ascoltino](https://github.com/soldatigabriele/ascoltino)
1.4.1, renamed, with two changes:

- **Audio files work.** Upstream only reads Telegram's `voice` field, so a
  voice note shared in from another app arrives as an `audio` file and is
  dropped with no reply and nothing in the app log.
- **An English-only model.** `parakeet-en` adds NVIDIA's Parakeet-TDT v2
  alongside upstream's multilingual v3 (see [Models](#models)).

The image is built `FROM` upstream's published image, pinned in
[`build.yaml`](build.yaml), and [`patch.py`](patch.py) rewrites `bot.py` and
`engines.py` at build time. Each replacement must match exactly once, so if
a future upstream version moves that code the build fails rather than
shipping a half-patched bot. No upstream code is copied into this repository.

## Quick start

1. In Telegram, message **@BotFather**, send `/newbot` and copy the token.
2. Message **@userinfobot** to get your numeric chat ID.
3. Install this app and set:
   - `bot_token` - the BotFather token
   - `allowed_chat_ids` - your chat ID (comma-separate several)
   - `admin_chat_id` - your chat ID again, for intruder alerts
4. Start it. The first start downloads the model (~600 MB for Parakeet).
5. Forward a voice note to your bot.

Full option reference and troubleshooting: [DOCS.md](DOCS.md).

## Models

| Model | Speed on CPU | Languages | Notes |
|---|---|---|---|
| `parakeet-en` (default) | Fast | English only | Parakeet-TDT v2; best English accuracy here |
| `parakeet` | Fast | 25 European, auto-detected | Parakeet-TDT v3, upstream's default |
| `turbo` | Slow | ~100 | Whisper large-v3 turbo; set `language` |
| `tiny` / `base` / `small` | Faster as they shrink | ~100 | Less accurate |
| `medium` / `large-v2` / `large-v3` | Slowest | ~100 | Most accurate Whisper sizes |

Models are cached in the app's data folder and kept out of backups.

## Security

- **Set `allowed_chat_ids`.** Leave it empty and anyone who finds the bot's
  username can use your CPU to transcribe their audio.
- With `admin_chat_id` set, you get one Telegram alert per unknown chat that
  sends the bot audio.
- Anyone with the bot token can control the bot. If it leaks, send `/revoke`
  to @BotFather and paste the new token in.

[aarch64-shield]: https://img.shields.io/badge/aarch64-yes-green.svg
[amd64-shield]: https://img.shields.io/badge/amd64-yes-green.svg
