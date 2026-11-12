"""integration_installer: copies the bundled integration when missing or a different version,
and the bundled integration itself is well-formed (manifest, services, translations)."""

import json
import os

import yaml

import integration_installer as inst

HERE = os.path.dirname(os.path.abspath(__file__))
REAL = os.path.join(HERE, "..", "integration", "tube_display")


def _bundle(tmp_path, version="1.0"):
    src = tmp_path / "bundle" / "tube_display"
    src.mkdir(parents=True)
    (src / "manifest.json").write_text(json.dumps({"domain": "tube_display", "version": version}))
    (src / "__init__.py").write_text("# v" + version)
    return str(src)


def test_installs_when_missing(tmp_path):
    root = tmp_path / "custom_components"
    root.mkdir()
    assert inst.install(_bundle(tmp_path), str(root)) == ("1.0", False)
    assert (root / "tube_display" / "__init__.py").read_text() == "# v1.0"


def test_same_version_untouched(tmp_path):
    root = tmp_path / "custom_components"
    root.mkdir()
    b = _bundle(tmp_path)
    inst.install(b, str(root))
    (root / "tube_display" / "__init__.py").write_text("local edit")
    assert inst.install(b, str(root)) == (None, True)   # nothing changed, but it was installed
    assert (root / "tube_display" / "__init__.py").read_text() == "local edit"


def test_other_version_replaced(tmp_path):
    root = tmp_path / "custom_components"
    (root / "tube_display").mkdir(parents=True)
    (root / "tube_display" / "manifest.json").write_text(json.dumps({"version": "0.1"}))
    (root / "tube_display" / "stale.py").write_text("x")
    assert inst.install(_bundle(tmp_path, "2.0"), str(root)) == ("2.0", True)
    assert not (root / "tube_display" / "stale.py").exists()


def test_no_custom_components_folder(tmp_path):
    assert inst.install(_bundle(tmp_path), str(tmp_path / "missing")) == (None, False)


def test_real_integration_is_consistent():
    manifest = json.load(open(os.path.join(REAL, "manifest.json")))
    assert manifest["domain"] == "tube_display" and manifest["config_flow"] is True
    services = yaml.safe_load(open(os.path.join(REAL, "services.yaml")))
    strings = json.load(open(os.path.join(REAL, "strings.json")))
    en = json.load(open(os.path.join(REAL, "translations", "en.json")))
    assert strings == en
    assert set(services) == set(strings["services"])
    for name, svc in services.items():
        assert set(svc.get("fields", {})) == set(strings["services"][name]["fields"]), name
    src = open(os.path.join(REAL, "__init__.py")).read()
    for name in services:
        assert f'"{name}"' in open(os.path.join(REAL, "const.py")).read()
    assert "PATHS" in src
    # every file compiles
    for f in os.listdir(REAL):
        if f.endswith(".py"):
            compile(open(os.path.join(REAL, f)).read(), f, "exec")
