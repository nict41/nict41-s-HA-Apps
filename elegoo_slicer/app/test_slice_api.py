import json
import stat
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import slice_api
from slice_api import PresetIndex, Settings, SliceError, Slicer, gcode_stats, make_handler


def write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) if not isinstance(data, str) else data)
    return path


@pytest.fixture
def profiles(tmp_path):
    system = tmp_path / "system"
    user = tmp_path / "user"
    write(system / "Elegoo/machine/common.json",
          {"type": "machine", "name": "common", "instantiation": "false",
           "nozzle_diameter": ["0.4"], "bed_temp": "60", "printable_height": "256"})
    write(system / "Elegoo/machine/cc2.json",
          {"type": "machine", "name": "CC2 0.4", "inherits": "common", "instantiation": "true",
           "bed_temp": "55"})
    write(system / "Elegoo/process/std.json",
          {"type": "process", "name": "0.20mm Standard", "instantiation": "true",
           "layer_height": "0.2", "wall_loops": "2",
           "compatible_printers": ["CC2 0.4"], "compatible_printers_condition": ""})
    write(system / "Elegoo/filament/pla.json",
          {"type": "filament", "name": "Generic PLA", "instantiation": "true",
           "nozzle_temperature": ["220"], "compatible_printers": ["CC2 0.4"]})
    write(system / "Elegoo/machine/model.json", {"type": "machine_model", "name": "CC2"})
    # User presets: no "type", filed by folder, diff against a system preset.
    write(user / "default/machine/mine.json",
          {"from": "User", "inherits": "CC2 0.4", "name": "My CC2", "printable_height": "250"})
    write(user / "default/process/mine.json",
          {"from": "User", "inherits": "0.20mm Standard", "name": "My process", "wall_loops": "4"})
    write(user / "default/filament/esun.json",
          {"from": "User", "inherits": "Generic PLA", "name": "Esun PLA",
           "nozzle_temperature": ["200"]})
    return system, user


def test_resolve_walks_the_whole_chain(profiles):
    idx = PresetIndex([profiles[0]], [profiles[1]])
    machine = idx.resolve("machine", "My CC2")
    assert machine["printable_height"] == "250"      # user value
    assert machine["bed_temp"] == "55"               # middle of the chain
    assert machine["nozzle_diameter"] == ["0.4"]     # root of the chain
    assert machine["name"] == "My CC2"
    assert machine["type"] == "machine"
    assert machine["instantiation"] == "true"
    # Still names its concrete system ancestor, skipping the abstract root.
    assert machine["inherits"] == "CC2 0.4"
    assert machine["from"] == "User"


def test_resolving_a_system_preset_marks_it_system(profiles):
    machine = PresetIndex([profiles[0]], [profiles[1]]).resolve("machine", "CC2 0.4")
    assert machine["from"] == "system"
    assert machine["inherits"] == ""
    assert machine["nozzle_diameter"] == ["0.4"]
    assert slice_api.system_printer_name(machine) == "CC2 0.4"


def test_resolve_drops_compatibility_lists(profiles):
    idx = PresetIndex([profiles[0]], [profiles[1]])
    process = idx.resolve("process", "My process")
    assert process["wall_loops"] == "4"
    assert process["layer_height"] == "0.2"
    assert "compatible_printers" not in process
    assert "compatible_printers_condition" not in process
    assert "compatible_printers" not in idx.resolve("filament", "Esun PLA")


def test_user_preset_replaces_system_preset_of_same_name(profiles):
    system, user = profiles
    write(user / "default/process/override.json",
          {"name": "0.20mm Standard", "layer_height": "0.3"})
    idx = PresetIndex([system], [user])
    assert idx.resolve("process", "0.20mm Standard")["layer_height"] == "0.3"


def test_first_user_root_wins(profiles, tmp_path):
    system, user = profiles
    first = tmp_path / "first"
    write(first / "process/mine.json", {"name": "My process", "wall_loops": "9"})
    idx = PresetIndex([system], [first, user])
    assert idx.resolve("process", "My process")["wall_loops"] == "9"


def test_unknown_preset_is_not_found(profiles):
    idx = PresetIndex([profiles[0]], [profiles[1]])
    with pytest.raises(SliceError) as err:
        idx.resolve("process", "nope")
    assert err.value.status == 404


def test_missing_parent_is_reported(profiles):
    system, user = profiles
    write(user / "process/orphan.json", {"name": "orphan", "inherits": "gone"})
    with pytest.raises(SliceError, match="'gone'"):
        PresetIndex([system], [user]).resolve("process", "orphan")


def test_inheritance_cycle_is_an_error(profiles):
    system, user = profiles
    write(user / "process/a.json", {"name": "a", "inherits": "b"})
    write(user / "process/b.json", {"name": "b", "inherits": "a"})
    with pytest.raises(SliceError, match="inherits from itself"):
        PresetIndex([system], [user]).resolve("process", "a")


def test_names_hides_abstract_presets_and_lists_user_first(profiles):
    idx = PresetIndex([profiles[0]], [profiles[1]])
    names = idx.names("machine")
    assert names[0] == {"name": "My CC2", "user": True}
    assert {"name": "common", "user": False} not in names
    assert all(n["name"] != "CC2" for n in names)  # machine_model is not a preset
    assert [n["name"] for n in idx.names("process", contains="STANDARD")] == ["0.20mm Standard"]


GCODE = """; HEADER_BLOCK_START
; generated by ElegooSlicer 1.5.3.5
; total layer number: 378
; max_z_height: 75.60
; HEADER_BLOCK_END
; thumbnail begin 144x144 10
; abc
; thumbnail end
G1 X1
; filament used [mm] = 27110.49
; total filament used [g] = 81.51
; estimated printing time (normal mode) = 2h 17m 3s
"""


def test_gcode_stats(tmp_path):
    stats = gcode_stats(write(tmp_path / "a.gcode", GCODE))
    assert stats["layers"] == 378
    assert stats["max_z_mm"] == 75.6
    assert stats["filament_g"] == 81.51
    assert stats["filament_mm"] == [27110.49]
    assert stats["print_time"] == "2h 17m 3s"
    assert stats["print_time_s"] == 2 * 3600 + 17 * 60 + 3
    assert stats["thumbnail"] is True


FAKE_SLICER = """#!{python}
import json, sys, zipfile
from pathlib import Path
args = sys.argv[1:]
Path(args[args.index("--datadir") + 1], "argv.json").write_text(json.dumps(args))
out = Path(args[args.index("--outputdir") + 1])
mode = "{mode}"
if mode == "gcode":
    (out / "plate_1.gcode").write_text({gcode!r})
elif mode == "3mf":
    with zipfile.ZipFile(out / args[args.index("--export-3mf") + 1], "w") as zf:
        zf.writestr("Metadata/plate_1.gcode", {gcode!r})
print("sliced")
sys.exit({code})
"""


def make_slicer(tmp_path, profiles, mode="gcode", code=0):
    fake = tmp_path / "fake-slicer"
    fake.write_text(FAKE_SLICER.format(python=sys.executable, mode=mode, gcode=GCODE, code=code))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    share = tmp_path / "share"
    write(share / "models/part.stl", "solid x\nendsolid x\n")
    settings = Settings({
        "SLICER_BIN": str(fake),
        "SLICER_SYSTEM_PROFILES": str(profiles[0]),
        "SLICER_USER_PROFILES": str(profiles[1]),
        "SLICE_ALLOWED_ROOTS": str(share),
        "SLICE_WORK_DIR": str(tmp_path / "work"),
    })
    return Slicer(settings), share


def request(share, **extra):
    body = {
        "model": str(share / "models/part.stl"),
        "machine": "My CC2",
        "process": "My process",
        "filaments": ["Esun PLA"],
    }
    body.update(extra)
    return body


def test_slice_writes_gcode_next_to_model(tmp_path, profiles):
    slicer, share = make_slicer(tmp_path, profiles)
    result = slicer.slice(request(share))
    assert result["ok"] is True
    assert result["gcode"] == str(share / "models/part.gcode")
    assert Path(result["gcode"]).read_text() == GCODE
    assert result["stats"]["filament_g"] == 81.51

    job = Path(result["job_dir"])
    machine = json.loads((job / "machine.json").read_text())
    assert machine["printable_height"] == "250" and machine["nozzle_diameter"] == ["0.4"]
    # The process and filament must name the machine's system preset, or the
    # command line refuses the combination (exit -17).
    process = json.loads((job / "process.json").read_text())
    filament = json.loads((job / "filament_1.json").read_text())
    assert machine["inherits"] == "CC2 0.4"
    assert process["compatible_printers"] == ["CC2 0.4"]
    assert filament["compatible_printers"] == ["CC2 0.4"]
    assert process["compatible_printers_condition"] == ""
    argv = json.loads((job / "datadir/argv.json").read_text())
    assert argv[argv.index("--load-settings") + 1] == f"{job}/machine.json;{job}/process.json"
    assert argv[argv.index("--load-filaments") + 1] == f"{job}/filament_1.json"
    assert argv[argv.index("--slice") + 1] == "0"
    assert argv[argv.index("--debug") + 1] == "3"
    assert argv[argv.index("--export-3mf") + 1] == "result.3mf"
    assert argv[-1] == str(share / "models/part.stl")


def test_slice_falls_back_to_gcode_inside_3mf(tmp_path, profiles):
    slicer, share = make_slicer(tmp_path, profiles, mode="3mf")
    result = slicer.slice(request(share, output=str(share / "out/x.gcode")))
    assert result["ok"] is True
    assert Path(result["gcode"]).read_text() == GCODE


def test_slice_reports_failure_without_gcode(tmp_path, profiles):
    slicer, share = make_slicer(tmp_path, profiles, mode="none", code=3)
    result = slicer.slice(request(share))
    assert result["ok"] is False
    assert result["returncode"] == 3
    assert "gcode" not in result
    assert "sliced" in result["log_tail"]


def test_slice_applies_overrides_and_options(tmp_path, profiles):
    slicer, share = make_slicer(tmp_path, profiles)
    result = slicer.slice(request(
        share,
        filaments=["Esun PLA", "Generic PLA"],
        overrides={"process": {"wall_loops": "6"}, "filament": {"filament_colour": ["#000000"]}},
        arrange=True, orient=True, bed_type="High Temp Plate", plate=1,
    ))
    job = Path(result["job_dir"])
    assert json.loads((job / "process.json").read_text())["wall_loops"] == "6"
    for i in (1, 2):
        assert json.loads((job / f"filament_{i}.json").read_text())["filament_colour"] == ["#000000"]
    argv = json.loads((job / "datadir/argv.json").read_text())
    assert argv[argv.index("--curr-bed-type") + 1] == "High Temp Plate"
    assert argv[argv.index("--arrange") + 1] == "1"
    assert argv[argv.index("--orient") + 1] == "1"
    assert argv[argv.index("--slice") + 1] == "1"


@pytest.mark.parametrize("field,value,status", [
    ("model", "/etc/passwd", 403),
    ("model", "relative.stl", 400),
    ("output", "/etc/x.gcode", 403),
    ("output", "{share}/models/part.stl", 400),
])
def test_paths_outside_allowed_roots_are_refused(tmp_path, profiles, field, value, status):
    slicer, share = make_slicer(tmp_path, profiles)
    with pytest.raises(SliceError) as err:
        slicer.slice(request(share, **{field: value.format(share=share)}))
    assert err.value.status == status


def test_traversal_out_of_allowed_root_is_refused(tmp_path, profiles):
    slicer, share = make_slicer(tmp_path, profiles)
    write(tmp_path / "secret.stl", "x")
    with pytest.raises(SliceError) as err:
        slicer.slice(request(share, model=str(share / ".." / "secret.stl")))
    assert err.value.status == 403


def test_missing_model_and_bad_filaments(tmp_path, profiles):
    slicer, share = make_slicer(tmp_path, profiles)
    with pytest.raises(SliceError) as err:
        slicer.slice(request(share, model=str(share / "nope.stl")))
    assert err.value.status == 404
    with pytest.raises(SliceError, match="filaments"):
        slicer.slice(request(share, filaments=[]))


def test_old_jobs_are_pruned(tmp_path, profiles):
    slicer, share = make_slicer(tmp_path, profiles)
    slicer.settings.keep_jobs = 2
    work = slicer.settings.work_dir
    for stamp in ("20200101-000000-aaaaaa", "20200101-000001-bbbbbb", "20200101-000002-cccccc"):
        (work / f"job-{stamp}").mkdir(parents=True)
    result = slicer.slice(request(share))
    remaining = sorted(p.name for p in work.glob("job-*"))
    assert len(remaining) == 2
    assert Path(result["job_dir"]).name in remaining


def test_only_one_slice_at_a_time(tmp_path, profiles):
    slicer, share = make_slicer(tmp_path, profiles)
    slicer._lock.acquire()
    try:
        with pytest.raises(SliceError) as err:
            slicer.slice(request(share))
        assert err.value.status == 409
    finally:
        slicer._lock.release()


@pytest.fixture
def server(tmp_path, profiles):
    slicer, share = make_slicer(tmp_path, profiles)
    slicer.settings.token = "s3cret"
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(slicer))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}", share
    httpd.shutdown()


def call(url, path, body=None, token="s3cret"):
    req = urllib.request.Request(url + path, data=None if body is None else json.dumps(body).encode())
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def test_http_requires_token(server):
    url, _ = server
    assert call(url, "/health", token=None)[0] == 401
    assert call(url, "/health", token="wrong")[0] == 401
    status, body = call(url, "/health")
    assert status == 200 and body["slicer_present"] is True


def test_http_presets_resolve_and_slice(server):
    url, share = server
    status, body = call(url, "/presets?type=filament")
    assert status == 200 and body["presets"][0] == {"name": "Esun PLA", "user": True}
    status, body = call(url, "/resolve?type=filament&name=Esun%20PLA")
    assert status == 200 and body["nozzle_temperature"] == ["200"]
    status, body = call(url, "/slice", request(share))
    assert status == 200 and body["ok"] is True
    assert call(url, "/presets?type=bogus")[0] == 400
    assert call(url, "/nope")[0] == 404
