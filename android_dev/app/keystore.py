import json
import os
import re
import secrets
import process
from config import DATA

ROOT = DATA / "keystores"
VALID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _files(name):
    if not VALID.fullmatch(name): raise ValueError("Invalid keystore name")
    return ROOT / f"{name}.jks", ROOT / f"{name}.json"


def fingerprint(path):
    p = process.run(["keytool", "-list", "-v", "-keystore", str(path), "-storepass:env", "STORE_PASSWORD"], env={**os.environ, "STORE_PASSWORD": _readpass(path)}, timeout=30, max_bytes=8000)
    if p.returncode: raise RuntimeError((p.stderr or p.stdout)[-2000:])
    for line in p.stdout.splitlines():
        if "SHA256:" in line: return line.split("SHA256:", 1)[1].strip()
    raise RuntimeError("keytool did not return SHA-256 fingerprint")


def _readpass(path): return json.loads(path.with_suffix(".json").read_text())["store_password"]


def create(name, alias="release", dname=None):
    path, metadata = _files(name); ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists() or metadata.exists(): raise ValueError("Refusing to overwrite an existing keystore")
    # PKCS12 has one password: keytool ignores -keypass, so a second one would not open the key.
    password = secrets.token_urlsafe(32)
    env = {**os.environ, "STORE_PASSWORD": password}
    argv = ["keytool", "-genkeypair", "-v", "-keystore", str(path), "-storetype", "PKCS12", "-storepass:env", "STORE_PASSWORD", "-alias", alias, "-keyalg", "RSA", "-keysize", "4096", "-validity", "10000", "-dname", dname or "CN=Android Release, OU=Android, O=Local, L=Local, ST=Local, C=US"]
    p = process.run(argv, env=env, timeout=60, max_bytes=8000)
    if p.returncode:
        path.unlink(missing_ok=True); raise RuntimeError((p.stderr or p.stdout)[-2000:])
    with os.fdopen(os.open(metadata, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
        json.dump({"alias": alias, "store_password": password, "key_password": password}, output, indent=2)
    os.chmod(path, 0o600)
    return {"name": name, "fingerprint": fingerprint(path)}


def listing():
    result = []
    for meta in ROOT.glob("*.json"):
        name = meta.stem; path = ROOT / f"{name}.jks"
        if path.exists():
            result.append({"name": name, "fingerprint": fingerprint(path)})
    return result
