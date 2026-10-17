# Ascoltino

## Setup

1. In Telegram, message **@BotFather**, send `/newbot` and copy the token.
2. Get your numeric chat ID from **@userinfobot**.
3. Configure the app:
   - `bot_token`: the BotFather token.
   - `allowed_chat_ids`: your chat ID (comma-separate several; group IDs are
     negative). Leave this empty and the bot answers anyone who finds it.
   - `admin_chat_id`: your chat ID, to get a startup message and a one-off
     alert whenever a chat that isn't allowed sends audio.
4. Start the app. The first start downloads the model (~600 MB for Parakeet).
5. Send or forward a voice note or audio file to the bot.

## What it transcribes

- Voice notes, recorded in Telegram or forwarded.
- Audio files (mp3, m4a, ogg...), e.g. a voice note shared from WhatsApp.
- Round video messages.
- Files sent as a document, if Telegram reports an `audio/*` type.

The Telegram Bot API only lets bots download files up to 20 MB.

## Models

- `parakeet` (default): NVIDIA Parakeet-TDT, fast on CPU. Detects the language
  itself, across 25 European languages.
- Whisper sizes (`tiny` ... `large-v3`, `turbo`): larger is more accurate but
  slower. Set `language` for these.

## Logs

The app log only shows the startup banner. Per-message logs go to
`/data/logs/bot.log` inside the app container.
