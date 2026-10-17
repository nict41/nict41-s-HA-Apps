"""Patch the upstream Ascoltino app at image build time.

Two changes to the upstream image's /app:

1. bot.py: transcribe audio files, not just voice notes. Upstream only looks at
   a message's `voice` field. A voice note re-shared from another app
   (WhatsApp, a recorder, the Files app) reaches Telegram as an `audio` file or
   a `document`, so the bot silently drops it.
2. engines.py: add a `parakeet-en` model, NVIDIA's English-only Parakeet-TDT
   0.6B v2, alongside upstream's multilingual v3 (`parakeet`). v2 scores
   slightly better on English. onnx-asr 0.12.0 in the upstream image already
   supports it.

Every replacement must match exactly once, so an upstream change to the code
around it fails the image build instead of shipping a half-patched app.
"""
import os
import sys

APP = sys.argv[1] if len(sys.argv) > 1 else "/app"

AUDIO_HELPER = '''def audio_attachment(message):
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

PATCHES = {
    "bot.py": [
        ("def poller(jobs):", AUDIO_HELPER),
        ('                    if "voice" in message:\n',
         "                    if audio_attachment(message):\n"),
        ('                if "voice" not in message:\n',
         "                if not audio_attachment(message):\n"),
        ('tg.download_voice(message["voice"]["file_id"], path)',
         'tg.download_voice(audio_attachment(message)["file_id"], path)'),
    ],
    "engines.py": [
        ('    "parakeet": "nemo-parakeet-tdt-0.6b-v3",\n',
         '    "parakeet": "nemo-parakeet-tdt-0.6b-v3",\n'
         '    "parakeet-en": "nemo-parakeet-tdt-0.6b-v2",\n'),
    ],
}


def patch(path, replacements):
    with open(path) as f:
        source = f.read()
    for old, new in replacements:
        count = source.count(old)
        if count != 1:
            sys.exit(f"patch: {path}: expected 1 match, found {count}: {old.strip()!r}")
        source = source.replace(old, new)
    with open(path, "w") as f:
        f.write(source)


def main():
    for name, replacements in PATCHES.items():
        patch(os.path.join(APP, name), replacements)
    print("patch: audio files accepted; parakeet-en (English-only v2) model added")


if __name__ == "__main__":
    main()
