# Changelog

## 1.4.1-2

- Add a `parakeet-en` model: NVIDIA's English-only Parakeet-TDT 0.6B v2,
  which scores slightly better on English than the multilingual v3
  (`parakeet`). It is the new default for fresh installs; existing installs
  keep their chosen model until it's changed in the app's configuration.
- The build patch is now `patch.py` and covers both `bot.py` and `engines.py`.

## 1.4.1-1

- First release: upstream Ascoltino 1.4.1, patched to also transcribe audio
  files (`audio`), round video messages (`video_note`) and audio documents.
  Upstream only handles `voice`, so a voice note shared in from another app
  arrived as an mp3 `audio` file and was silently ignored.
