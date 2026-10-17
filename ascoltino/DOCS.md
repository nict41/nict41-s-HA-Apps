# Transcribe-a-bot

## Setup

1. In Telegram, message **@BotFather**, send `/newbot` and copy the token.
2. Get your numeric chat ID from **@userinfobot**.
3. Configure the app (options below), at minimum `bot_token` and
   `allowed_chat_ids`.
4. Start the app. The first start downloads the model (~600 MB for Parakeet),
   so give it a minute or two. The app log shows `✅ Ready!` when it's done.
5. Send or forward a voice note or audio file to the bot.

## Options

| Option | Default | What it does |
|---|---|---|
| `bot_token` | - | Token from @BotFather. Required. |
| `model` | `parakeet-en` | `parakeet-en` (English only), `parakeet` (25 European languages), or a Whisper size: `tiny`, `base`, `small`, `medium`, `turbo`, `large-v2`, `large-v3`. |
| `language` | `en` | Two-letter code used by Whisper. The Parakeet models ignore it. |
| `beam_size` | `1` | Whisper beam search width (1-10). Higher can be slightly more accurate and is slower. Ignored by Parakeet. |
| `vad_filter` | `false` | Skip silence before transcribing (Whisper). |
| `threads` | `0` | CPU threads to use; `0` means all cores. Set one lower than your core count if other apps need CPU headroom. |
| `show_footer` | `true` | Add a line with timing and model under each transcript. |
| `bot_name` | - | Optional name shown in the footer. |
| `admin_chat_id` | - | Chat that gets a one-off alert for each unknown chat that sends audio. |
| `allowed_chat_ids` | - | Comma-separated chat IDs allowed to use the bot. Group IDs are negative. **Empty means anyone.** |

## What it transcribes

- Voice notes, recorded in Telegram or forwarded.
- Audio files (mp3, m4a, ogg...), e.g. a voice note shared from WhatsApp.
- Round video messages.
- Files sent as a document, if Telegram reports an `audio/*` type.

Telegram only lets bots download files up to 20 MB.

## Troubleshooting

**No reply at all.**

- Check the chat ID in `allowed_chat_ids` is yours (ask @userinfobot). If
  `admin_chat_id` is set, a blocked chat triggers an alert there.
- Make sure only **one** copy of the bot is running with this token. Two apps,
  or this app plus the upstream Ascoltino app, polling the same token each receive
  only some of the messages, so notes go missing at random.
- Messages that arrive while the app is stopped are kept by Telegram for up
  to 24 hours and processed when it starts again.

**"❌ Could not read the audio file."** The file isn't audio the decoder
understands. Re-send it as a voice note or an mp3.

**Wrong language or poor accuracy.** `parakeet-en` only understands English;
switch to `parakeet` for 25 European languages. For anything else, switch `model` to `turbo` or `small` and set `language`.

## Logs

The app's log tab only shows the startup banner, including the model and the
allowed chat list. Per-message logs (received, downloaded, transcribed,
ignored) are written to `/data/logs/bot.log` inside the app container.
