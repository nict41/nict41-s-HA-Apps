"""Teach the upstream Ascoltino bot to transcribe audio files, not just voice notes.

Upstream only looks at a message's `voice` field. A voice note re-shared from
another app (WhatsApp, a recorder, the Files app) reaches Telegram as an `audio`
file or a `document`, so the bot silently drops it. This rewrites /app/bot.py
in the upstream image at build time to accept any of those.

Every replacement must match exactly once, so an upstream change to the code
around it fails the image build instead of shipping a half-patched bot.
"""
import sys

BOT = sys.argv[1] if len(sys.argv) > 1 else "/app/bot.py"

HELPER = '''def audio_attachment(message):
    """The transcribable attachment on a message, or None.

    Voice notes arrive as `voice`, but audio shared from other apps arrives as
    `audio`, round videos as `video_note`, and some files as an audio `document`.
    """
    for kind in ("voice", "audio", "video_note"):
        if kind in message:
            return message[kind]
    document = message.get("document")
    if document and document.get("mime_type", "").startswith("audio/"):
        return document
    return None


def poller(jobs):'''

REPLACEMENTS = [
    ("def poller(jobs):", HELPER),
    ('                    if "voice" in message:\n',
     "                    if audio_attachment(message):\n"),
    ('                if "voice" not in message:\n',
     "                if not audio_attachment(message):\n"),
    ('tg.download_voice(message["voice"]["file_id"], path)',
     'tg.download_voice(audio_attachment(message)["file_id"], path)'),
]


def main():
    with open(BOT) as f:
        source = f.read()
    for old, new in REPLACEMENTS:
        count = source.count(old)
        if count != 1:
            sys.exit(f"patch_audio: expected 1 match, found {count}: {old.strip()!r}")
        source = source.replace(old, new)
    with open(BOT, "w") as f:
        f.write(source)
    print("patch_audio: bot.py now accepts voice, audio, video_note and audio documents")


if __name__ == "__main__":
    main()
