import re
from pathlib import Path
from config import PIO_ROOTS, PROJECTS

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def project_path(name: str, create=False) -> Path:
    if not NAME.fullmatch(name):
        raise ValueError("Invalid project name")
    root = PROJECTS.resolve()
    candidate = root / name
    if create:
        candidate.mkdir(parents=True, exist_ok=True)
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("Project path escapes the projects root")
    return resolved


def project_file(path: str) -> Path:
    root = PROJECTS.resolve()
    resolved = Path(path).resolve(strict=True)
    if root not in resolved.parents:
        raise ValueError("APK path must be inside the projects root")
    return resolved


def pio_project_path(path: str) -> Path:
    try: resolved = Path(path).resolve(strict=True)
    except (OSError, RuntimeError) as exc: raise ValueError("PlatformIO project path does not exist") from exc
    roots = [root.resolve() for root in PIO_ROOTS]
    if not any(resolved == root or root in resolved.parents for root in roots):
        raise ValueError("PlatformIO project path is outside the allowed roots")
    if not (resolved / "platformio.ini").is_file():
        raise ValueError("PlatformIO project has no platformio.ini")
    return resolved
