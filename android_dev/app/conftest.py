import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("MCP_TOKEN", "test-token-that-is-at-least-32-characters")
# config.py reads these at import; /data does not exist on the CI runner.
_root = Path(tempfile.mkdtemp(prefix="android_dev_test_"))
os.environ["DATA_ROOT"] = str(_root / "data")
os.environ["PROJECTS_ROOT"] = str(_root / "projects")
os.environ["SCREENSHOTS_ROOT"] = str(_root / "screenshots")
os.environ["ANDROID_HOME"] = str(_root / "data" / "android-sdk")
os.environ["GRADLE_USER_HOME"] = str(_root / "data" / "gradle")
os.environ["HOSTNAME"] = "beb500c8-android-dev"
sys.path.insert(0, str(Path(__file__).parent))
