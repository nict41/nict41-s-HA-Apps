import os
import socket
from pathlib import Path

PROJECTS = Path(os.getenv("PROJECTS_ROOT", "/share/android_dev/projects"))
DATA = Path(os.getenv("DATA_ROOT", "/data"))
ANDROID_HOME = Path(os.getenv("ANDROID_HOME", str(DATA / "android-sdk")))
GRADLE_HOME = Path(os.getenv("GRADLE_USER_HOME", str(DATA / "gradle")))
GRADLE = DATA / "tools" / "gradle-9.8.2" / "bin" / "gradle"
SCREENSHOTS = Path(os.getenv("SCREENSHOTS_ROOT", "/share/android_dev/screenshots"))
# Popped so it is not inherited by Gradle builds, which run project code.
TOKEN = os.environ.pop("MCP_TOKEN", "")
TOKEN_VALID = len(TOKEN) >= 32
LICENSE_ACCEPTED = os.getenv("ACCEPT_ANDROID_SDK_LICENSE", "false").lower() == "true"
HEAP_GB = int(os.getenv("GRADLE_MAX_HEAP_GB", "6"))
WORKERS = int(os.getenv("GRADLE_MAX_WORKERS", "3"))
JOB_TIMEOUT = int(os.getenv("GRADLE_JOB_TIMEOUT_MINUTES", "60")) * 60
HOSTNAME = os.getenv("HOSTNAME") or socket.gethostname()
