import hashlib
import os
import shutil
import tempfile
import threading
import urllib.request
import zipfile
from pathlib import Path
import process

from config import ANDROID_HOME, GRADLE, LICENSE_ACCEPTED

CMDLINE_URL = "https://dl.google.com/android/repository/commandlinetools-linux-16111833_latest.zip"
CMDLINE_SIZE = 181052239
CMDLINE_SHA1 = "e025545c62a8e64c7559119566a569fb1dec5f60"
GRADLE_URL = "https://services.gradle.org/distributions/gradle-9.8.2-bin.zip"
GRADLE_SHA256 = "21bd5da2de49b642904077f38c0efdc7778e92b3ed0889392dca61b97e389607"
GRADLE_DIR = GRADLE.parent.parent
state = {"setup": "waiting_for_license" if not LICENSE_ACCEPTED else "pending", "last_error": None}
lock = threading.Lock()


def fetch(url: str, path: Path, expected_size: int | None = None, algorithm: str = "sha256", digest: str = ""):
    h = hashlib.new(algorithm)
    size = 0
    with urllib.request.urlopen(url, timeout=90) as response, path.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            output.write(chunk)
            h.update(chunk)
            size += len(chunk)
    if expected_size is not None and size != expected_size:
        raise ValueError(f"Download size mismatch: expected {expected_size}, got {size}")
    if digest and h.hexdigest() != digest:
        raise ValueError(f"Checksum mismatch for {url}")


def _extract(archive: Path, destination: Path):
    # ZipFile.extractall drops Unix modes, which would leave bin/gradle unrunnable.
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            extracted = Path(zf.extract(info, destination))
            mode = info.external_attr >> 16
            if mode and not info.is_dir():
                extracted.chmod(mode & 0o777)


def _installed():
    return (ANDROID_HOME / "platform-tools" / "adb").exists() and GRADLE.exists()


def _install_zip(url: str, target: Path, size=None, algorithm="sha256", digest="", strip_dir=None):
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as tmp:
        tmp_path = Path(tmp)
        archive = tmp_path / "download.zip"
        fetch(url, archive, size, algorithm, digest)
        extracted = tmp_path / "extracted"
        extracted.mkdir()
        _extract(archive, extracted)
        source = extracted / strip_dir if strip_dir else extracted
        staged = tmp_path / "install"
        shutil.copytree(source, staged)
        backup = tmp_path / "previous"
        if target.exists():
            os.replace(target, backup)
        try:
            os.replace(staged, target)
        except Exception:
            if backup.exists(): os.replace(backup, target)
            raise
        if backup.exists(): shutil.rmtree(backup)


def sdk_setup():
    if not LICENSE_ACCEPTED:
        state.update(setup="waiting_for_license", last_error="Enable accept_android_sdk_license to download Android SDK tools or accept the Google licence.")
        return dict(state)
    # A setup already in progress keeps running; callers poll the state.
    if not lock.acquire(blocking=False):
        return dict(state)
    try:
        try:
            if _installed():
                state.update(setup="ready", last_error=None)
                return dict(state)
            state.update(setup="installing", last_error=None)
            cmdtools = ANDROID_HOME / "cmdline-tools" / "latest"
            sdkmanager = cmdtools / "bin" / "sdkmanager"
            if not sdkmanager.exists():
                _install_zip(CMDLINE_URL, cmdtools, CMDLINE_SIZE, "sha1", CMDLINE_SHA1, "cmdline-tools")
            if not (GRADLE_DIR / "bin" / "gradle").exists():
                _install_zip(GRADLE_URL, GRADLE_DIR, algorithm="sha256", digest=GRADLE_SHA256, strip_dir="gradle-9.8.2")
            env = os.environ.copy()
            env["ANDROID_HOME"] = str(ANDROID_HOME)
            lic = process.run([str(sdkmanager), "--licenses"], input="y\n" * 100, timeout=180, env=env, max_bytes=12000)
            if lic.returncode:
                raise RuntimeError((lic.stderr or lic.stdout)[-4000:])
            result = process.run([str(sdkmanager), "platform-tools"], timeout=300, env=env, max_bytes=12000)
            if result.returncode:
                raise RuntimeError((result.stderr or result.stdout)[-4000:])
            state.update(setup="ready", last_error=None)
        except Exception as exc:
            state.update(setup="error", last_error=str(exc)[:2000])
    finally:
        lock.release()
    return dict(state)


def start_setup():
    if LICENSE_ACCEPTED:
        threading.Thread(target=sdk_setup, name="sdk-setup", daemon=True).start()


def installed_packages():
    sdkmanager = ANDROID_HOME / "cmdline-tools/latest/bin/sdkmanager"
    if not sdkmanager.exists():
        return "SDK is not installed"
    p = process.run([str(sdkmanager), "--list_installed"], timeout=60, max_bytes=12000)
    return p.stdout
