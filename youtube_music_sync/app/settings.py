"""Add-on options, read from the environment the s6 run script exports."""

from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass, field

# Where a relative cookies_file is resolved from: the add-on's own config
# folder, which Home Assistant surfaces to the user as
# /addon_configs/<slug>/ over Samba or the File editor.
ADDON_CONFIG_DIR = "/config"

# Downloads are only ever written below one of these. A music_dir outside them
# would point at the add-on's own ephemeral container filesystem, where files
# would vanish on the next update without ever reaching the NAS.
ALLOWED_ROOTS = ("/media", "/share")

QUARANTINE_DIRNAME = ".yt_duplicates"


def _str(name: str, default: str = "") -> str:
    return (os.environ.get(name) or "").strip() or default


def _bool(name: str, default: bool = False) -> bool:
    raw = _str(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(float(_str(name)))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_str(name))
    except (TypeError, ValueError):
        return default


def _playlists() -> tuple[str, ...]:
    raw = _str("YMS_PLAYLISTS", "[]")
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        # Fall back to newline/comma separated, in case bashio handed us a
        # plain list rather than JSON.
        parsed = [p for p in raw.replace(",", "\n").splitlines()]
    if isinstance(parsed, str):
        parsed = [parsed]
    return tuple(str(p).strip() for p in parsed if str(p).strip())


@dataclass(frozen=True)
class MqttConfig:
    host: str
    port: int
    username: str = ""
    password: str = ""


@dataclass(frozen=True)
class Config:
    playlists: tuple[str, ...] = field(default_factory=tuple)
    music_dir: str = "/media/nas1/Music"
    poll_interval_minutes: int = 60
    dry_run: bool = True
    audio_format: str = "best"
    output_template: str = "%(title)s.%(ext)s"
    sponsorblock: bool = True
    embed_thumbnail: bool = True
    crop_thumbnail_square: bool = True
    duration_tolerance: float = 5.0
    title_similarity: float = 0.87
    on_duplicate: str = "quarantine"
    min_duration: int = 60
    max_duration: int = 0
    max_downloads_per_cycle: int = 20
    sleep_between_downloads: int = 5
    rate_limit: str = ""
    cookies_file: str = ""
    use_aria2c: bool = False
    extra_args: tuple[str, ...] = field(default_factory=tuple)
    log_level: str = "info"
    data_dir: str = "/data"
    mqtt: MqttConfig | None = None

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "state.db")

    @property
    def archive_path(self) -> str:
        """yt-dlp's own archive file, kept alongside the database.

        Belt and braces: the database is the source of truth for reporting,
        but handing yt-dlp its native archive means even a download started
        outside the normal flow will not be fetched twice.
        """
        return os.path.join(self.data_dir, "downloaded.txt")

    @property
    def tmp_dir(self) -> str:
        """Scratch space for in-progress downloads.

        Deliberately local rather than on the NAS: partial files and the
        thumbnail/remux shuffle over SMB are slow and leave debris behind on a
        failure. Only the finished file is moved to the library.
        """
        return os.path.join(self.data_dir, "tmp")

    @property
    def quarantine_dir(self) -> str:
        return os.path.join(self.music_dir, QUARANTINE_DIRNAME)

    @property
    def resolved_cookies_file(self) -> str:
        if not self.cookies_file:
            return ""
        if os.path.isabs(self.cookies_file):
            return self.cookies_file
        return os.path.join(ADDON_CONFIG_DIR, self.cookies_file)

    def problems(self) -> list[str]:
        """Configuration errors that should stop the add-on doing anything."""
        issues: list[str] = []
        if not self.playlists:
            issues.append(
                "No playlists configured - add at least one public playlist URL "
                "to the 'playlists' option."
            )
        normalized = os.path.normpath(self.music_dir)
        if not any(
            normalized == root or normalized.startswith(root + os.sep)
            for root in ALLOWED_ROOTS
        ):
            issues.append(
                f"music_dir ({self.music_dir}) must be under /media or /share, "
                "which is where Home Assistant network storage mounts appear. "
                "A path outside them would write into the add-on container and "
                "be lost on the next update."
            )
        elif normalized in ALLOWED_ROOTS:
            issues.append(
                f"music_dir ({self.music_dir}) is the mount root itself - point "
                "it at the music folder inside your NAS share."
            )
        cookies = self.resolved_cookies_file
        if cookies and not os.path.isfile(cookies):
            issues.append(f"cookies_file does not exist: {cookies}")
        return issues


def load() -> Config:
    mqtt_host = _str("YMS_MQTT_HOST")
    mqtt = None
    if mqtt_host:
        mqtt = MqttConfig(
            host=mqtt_host,
            port=_int("YMS_MQTT_PORT", 1883),
            username=_str("YMS_MQTT_USER"),
            password=_str("YMS_MQTT_PASS"),
        )

    return Config(
        playlists=_playlists(),
        music_dir=_str("YMS_MUSIC_DIR", "/media/nas1/Music"),
        poll_interval_minutes=_int("YMS_POLL_INTERVAL_MINUTES", 60),
        dry_run=_bool("YMS_DRY_RUN", True),
        audio_format=_str("YMS_AUDIO_FORMAT", "best"),
        output_template=_str("YMS_OUTPUT_TEMPLATE", "%(title)s.%(ext)s"),
        sponsorblock=_bool("YMS_SPONSORBLOCK", True),
        embed_thumbnail=_bool("YMS_EMBED_THUMBNAIL", True),
        crop_thumbnail_square=_bool("YMS_CROP_THUMBNAIL_SQUARE", True),
        duration_tolerance=_float("YMS_DURATION_TOLERANCE", 5.0),
        title_similarity=_float("YMS_TITLE_SIMILARITY", 0.87),
        on_duplicate=_str("YMS_ON_DUPLICATE", "quarantine"),
        min_duration=_int("YMS_MIN_DURATION", 60),
        max_duration=_int("YMS_MAX_DURATION", 0),
        max_downloads_per_cycle=_int("YMS_MAX_DOWNLOADS_PER_CYCLE", 20),
        sleep_between_downloads=_int("YMS_SLEEP_BETWEEN_DOWNLOADS", 5),
        rate_limit=_str("YMS_RATE_LIMIT"),
        cookies_file=_str("YMS_COOKIES_FILE"),
        use_aria2c=_bool("YMS_USE_ARIA2C", False),
        extra_args=tuple(shlex.split(_str("YMS_EXTRA_ARGS"))),
        log_level=_str("YMS_LOG_LEVEL", "info"),
        data_dir=_str("YMS_DATA_DIR", "/data"),
        mqtt=mqtt,
    )
