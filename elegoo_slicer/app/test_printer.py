"""The printer client against a fake Centauri Carbon 2.

The fake speaks just enough MQTT 3.1.1 and HTTP to stand in for the printer,
and answers with the message shapes captured from a real one (fw 02.01.01.07).
"""

import hashlib
import json
import socket
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import printer as printer_mod
from printer import Printer, PrinterError
from slice_api import Settings, SliceError, Slicer

SN = "TESTSN"
CODE = "abc123"


def _s16(b):
    return struct.pack("!H", len(b)) + b


def _rl(n):
    out = b""
    while True:
        d, n = n % 128, n // 128
        out += bytes([d | (0x80 if n else 0)])
        if not n:
            return out


class FakeCC2:
    def __init__(self):
        self.status = 1
        self.register_error = "ok"
        self.trays = [
            {"tray_id": 0, "status": 1, "filament_color": "#FFF242", "filament_type": "PLA", "filament_name": "PLA Basic"},
            {"tray_id": 1, "status": 1, "filament_color": "#000000", "filament_type": "PLA", "filament_name": "PLA Basic"},
            {"tray_id": 2, "status": 0, "filament_color": "#000000", "filament_type": "PLA", "filament_name": "PLA Basic"},
        ]
        self.files: dict[str, dict] = {}
        self.requests: list[tuple[int, dict]] = []
        self.registrations = 0
        self.start_goes_to = 2  # what status a START_PRINT moves the printer to
        self.homing_polls = 0  # status polls left before a home finishes
        self.nozzle_target = 0
        self.uploads: list[dict] = []
        self.upload_connections: set = set()
        self.upload_body = bytearray()

        self.mqtt = socket.socket()
        self.mqtt.bind(("127.0.0.1", 0))
        self.mqtt.listen()
        threading.Thread(target=self._accept, daemon=True).start()

        fake = self

        class Upload(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_PUT(self):
                fake.upload_connections.add(self.client_address)
                body = self.rfile.read(int(self.headers["Content-Length"]))
                fake.uploads.append({h: self.headers[h] for h in ("Content-Range", "X-File-Name", "X-File-MD5", "X-Token")})
                fake.upload_body += body
                start, rest = self.headers["Content-Range"][6:].split("-")
                end, total = rest.split("/")
                if int(end) + 1 == int(total):
                    fake.files[self.headers["X-File-Name"]] = {
                        "size": len(fake.upload_body), "layer": 10, "print_time": 60,
                        "color_map": [{"color": "#000000", "name": "PLA", "t": 0}],
                    }
                reply = json.dumps({"error_code": 0, "offset": int(end)}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Upload)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    @property
    def mqtt_port(self):
        return self.mqtt.getsockname()[1]

    @property
    def http_port(self):
        return self.http.server_port

    def _accept(self):
        while True:
            try:
                conn, _ = self.mqtt.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _read(self, conn):
        head = conn.recv(1)
        if not head:
            raise EOFError
        mult, n = 1, 0
        while True:
            b = conn.recv(1)[0]
            n += (b & 127) * mult
            mult *= 128
            if not b & 128:
                break
        data = b""
        while len(data) < n:
            data += conn.recv(n - len(data))
        return head[0], data

    def _pub(self, conn, topic, payload):
        body = _s16(topic.encode()) + json.dumps(payload).encode()
        conn.sendall(bytes([0x30]) + _rl(len(body)) + body)

    def _serve(self, conn):
        try:
            while True:
                kind, data = self._read(conn)
                t = kind >> 4
                if t == 1:  # CONNECT
                    pos = 10
                    cid_len = struct.unpack("!H", data[pos:pos + 2])[0]
                    pos += 2 + cid_len
                    pos += 2 + struct.unpack("!H", data[pos:pos + 2])[0]  # username
                    pw = data[pos + 2:pos + 2 + struct.unpack("!H", data[pos:pos + 2])[0]].decode()
                    conn.sendall(b"\x20\x02\x00" + (b"\x00" if pw == CODE else b"\x05"))
                elif t == 8:  # SUBSCRIBE
                    conn.sendall(b"\x90\x03" + data[:2] + b"\x00")
                elif t == 3:  # PUBLISH
                    tl = struct.unpack("!H", data[:2])[0]
                    topic = data[2:2 + tl].decode()
                    msg = json.loads(data[2 + tl:])
                    if topic.endswith("/api_register"):
                        self.registrations += 1
                        self._pub(conn, f"elegoo/{SN}/{msg['request_id']}/register_response",
                                  {"client_id": msg["client_id"], "error": self.register_error})
                    elif topic.endswith("/api_request"):
                        cid = topic.split("/")[2]
                        self.requests.append((msg["method"], msg.get("params", {})))
                        self._pub(conn, f"elegoo/{SN}/{cid}/api_response",
                                  {"id": msg["id"], "method": msg["method"], "result": self._answer(msg["method"], msg.get("params", {}))})
                elif t == 14:  # DISCONNECT
                    conn.close()
                    return
        except (EOFError, OSError):
            return

    def _answer(self, method, params):
        if method == 1002:
            if self.status == 10:
                if self.homing_polls:
                    self.homing_polls -= 1
                else:
                    self.status = 1
            return {"error_code": 0, "machine_status": {"status": self.status, "sub_status": 0},
                    "print_status": {"filename": ""}, "gcode_move": {"z": 5.05},
                    "extruder": {"temperature": 25, "target": self.nozzle_target}, "heater_bed": {"temperature": 24}}
        if method == 1026:
            self.status, self.homing_polls, self.nozzle_target = 10, 2, 140
            return {"error_code": 0}
        if method == 1028:
            self.nozzle_target = params.get("extruder", self.nozzle_target)
            return {"error_code": 0}
        if method == 2005:
            return {"error_code": 0, "canvas_info": {"auto_refill": True, "canvas_list": [
                {"canvas_id": 0, "connected": 1, "tray_list": self.trays}]}}
        if method == 1046:
            f = self.files.get(params.get("filename"))
            return {"error_code": 0, "filename": params["filename"], **f} if f else {"error_code": 1009}
        if method == 1044:
            return {"error_code": 0, "file_list": [{"filename": n, **f} for n, f in self.files.items()]}
        if method == 1047:
            for n in params["file_path"]:
                self.files.pop(n, None)
            return {"error_code": 0}
        if method == 2004:
            return {"error_code": 0}
        if method == 1020:
            self.status = self.start_goes_to
            return {"error_code": 0}
        return {"error_code": 1}


@pytest.fixture
def cc2():
    fake = FakeCC2()
    yield fake
    fake.mqtt.close()
    fake.http.shutdown()


@pytest.fixture(autouse=True)
def fast_polls(monkeypatch):
    monkeypatch.setattr(printer_mod, "POLL_SECONDS", 0)


@pytest.fixture
def printer(cc2):
    return Printer("127.0.0.1", SN, CODE, mqtt_port=cc2.mqtt_port, http_port=cc2.http_port)


def test_status_summarises_machine_and_trays(printer):
    st = printer.status()
    assert st["state"] == "idle" and st["idle"] is True
    assert st["canvas_connected"] is True
    assert st["trays"][1] == {"canvas_id": 0, "tray_id": 1, "loaded": True, "type": "PLA",
                              "name": "PLA Basic", "colour": "#000000"}
    assert st["trays"][2]["loaded"] is False


def test_too_many_clients_is_reported_as_such(cc2, printer):
    cc2.register_error = "too many clients"
    with pytest.raises(PrinterError, match="no free client slots") as err:
        printer.status()
    assert err.value.status == 503


def test_wrong_access_code(cc2):
    bad = Printer("127.0.0.1", SN, "nope", mqtt_port=cc2.mqtt_port, http_port=cc2.http_port)
    with pytest.raises(PrinterError, match="access code"):
        bad.status()


def test_unconfigured_printer():
    with pytest.raises(PrinterError, match="not configured"):
        Printer("", "", "")


def test_upload_chunks_over_one_connection(cc2, printer, tmp_path, monkeypatch):
    monkeypatch.setattr(printer_mod, "CHUNK", 1000)
    data = bytes(range(256)) * 10  # 2560 bytes -> 3 chunks
    gcode = tmp_path / "part.gcode"
    gcode.write_bytes(data)
    result = printer.upload(gcode, "part.gcode")
    assert [u["Content-Range"] for u in cc2.uploads] == ["bytes 0-999/2560", "bytes 1000-1999/2560", "bytes 2000-2559/2560"]
    md5 = hashlib.md5(data).hexdigest()
    assert {u["X-File-MD5"] for u in cc2.uploads} == {md5}
    assert {u["X-Token"] for u in cc2.uploads} == {CODE}
    assert len(cc2.upload_connections) == 1
    assert bytes(cc2.upload_body) == data
    assert result["detail"]["size"] == 2560


def upload(cc2, name="part.gcode"):
    cc2.files[name] = {"size": 10, "color_map": [{"color": "#000000", "name": "PLA", "t": 0}]}


def test_start_sends_what_the_slicer_sends(cc2, printer):
    upload(cc2)
    result = printer.start("part.gcode", [{"t": 0, "tray_id": 1}],
                           config={"print_layout": "A", "delay_video": True, "printer_check": True})
    assert result["started"] is True
    methods = [m for m, _ in cc2.requests]
    assert methods.index(2004) < methods.index(1020)
    params = dict(cc2.requests)[1020]
    assert params == {
        "config": {"bedlevel_force": False, "delay_video": True, "print_layout": "A", "printer_check": True,
                   "slot_map": [{"canvas_id": 0, "t": 0, "tray_id": 1}]},
        "filename": "part.gcode",
        "storage_media": "local",
    }
    assert cc2.registrations == 1  # the whole start is one client session


def test_start_refuses_when_not_idle(cc2, printer):
    upload(cc2)
    cc2.status = 2
    with pytest.raises(PrinterError, match="printing, not idle") as err:
        printer.start("part.gcode", [{"t": 0, "tray_id": 1}])
    assert err.value.status == 409
    assert 1020 not in [m for m, _ in cc2.requests]


def test_start_refuses_missing_file_unmapped_filament_and_empty_tray(cc2, printer):
    with pytest.raises(PrinterError, match="no file"):
        printer.start("missing.gcode", [{"t": 0, "tray_id": 1}])
    cc2.files["two.gcode"] = {"size": 1, "color_map": [{"t": 0}, {"t": 1}]}
    with pytest.raises(PrinterError, match=r"no tray for filament\(s\) \[1\]"):
        printer.start("two.gcode", [{"t": 0, "tray_id": 1}])
    upload(cc2)
    with pytest.raises(PrinterError, match="tray 2 is empty"):
        printer.start("part.gcode", [{"t": 0, "tray_id": 2}])
    assert 1020 not in [m for m, _ in cc2.requests]


def test_start_that_never_begins_is_an_error(cc2, printer):
    upload(cc2)
    cc2.start_goes_to = 1
    with pytest.raises(PrinterError, match="still idle"):
        printer.start("part.gcode", [{"t": 0, "tray_id": 1}], verify_seconds=0)


def test_delete(cc2, printer):
    upload(cc2)
    printer.delete("part.gcode")
    assert "part.gcode" not in cc2.files


# ── through the API ─────────────────────────────────────────────────────────


@pytest.fixture
def api(cc2, tmp_path, monkeypatch):
    share = tmp_path / "share"
    share.mkdir()
    settings = Settings({
        "SLICE_ALLOWED_ROOTS": str(share), "SLICE_WORK_DIR": str(tmp_path / "work"),
        "SLICER_SYSTEM_PROFILES": str(tmp_path), "SLICER_USER_PROFILES": str(tmp_path),
        "PRINTER_HOST": "127.0.0.1", "PRINTER_SERIAL": SN, "PRINTER_ACCESS_CODE": CODE,
    })
    slicer = Slicer(settings)
    monkeypatch.setattr(slicer, "printer", lambda: Printer("127.0.0.1", SN, CODE, mqtt_port=cc2.mqtt_port, http_port=cc2.http_port))
    return slicer, share


def test_api_print_needs_confirm_and_plate(cc2, api):
    slicer, _ = api
    upload(cc2)
    body = {"filename": "part.gcode", "slot_map": [{"t": 0, "tray_id": 1}]}
    with pytest.raises(SliceError, match="confirm"):
        slicer.printer_call("POST", "/printer/print", {}, body)
    with pytest.raises(SliceError, match="plate"):
        slicer.printer_call("POST", "/printer/print", {}, {**body, "confirm": True})
    assert 1020 not in [m for m, _ in cc2.requests]


@pytest.mark.parametrize(("plate", "layout"), [("textured", "A"), ("smooth", "B")])
def test_api_print_maps_plate_and_defaults_timelapse_on(cc2, api, plate, layout):
    slicer, _ = api
    upload(cc2)
    slicer.printer_call("POST", "/printer/print", {}, {
        "filename": "part.gcode", "slot_map": [{"t": 0, "tray_id": 1}], "confirm": True, "plate": plate})
    config = dict(cc2.requests)[1020]["config"]
    assert config["print_layout"] == layout
    assert config["delay_video"] is True and config["printer_check"] is True


def test_api_upload_checks_paths(cc2, api):
    slicer, share = api
    (share / "a.gcode").write_text("G1\n")
    (share / "a.stl").write_text("solid\n")
    assert slicer.printer_call("POST", "/printer/upload", {}, {"gcode": str(share / "a.gcode")})["filename"] == "a.gcode"
    with pytest.raises(SliceError, match=".gcode"):
        slicer.printer_call("POST", "/printer/upload", {}, {"gcode": str(share / "a.stl")})
    with pytest.raises(SliceError, match="bare file name"):
        slicer.printer_call("POST", "/printer/upload", {}, {"gcode": str(share / "a.gcode"), "name": "../x.gcode"})
    with pytest.raises(SliceError) as err:
        slicer.printer_call("POST", "/printer/upload", {}, {"gcode": "/etc/passwd"})
    assert err.value.status == 403


def test_api_printer_errors_keep_their_status(cc2, api):
    slicer, _ = api
    cc2.status = 2
    upload(cc2)
    with pytest.raises(SliceError) as err:
        slicer.printer_call("POST", "/printer/print", {}, {
            "filename": "part.gcode", "slot_map": [{"t": 0, "tray_id": 1}], "confirm": True, "plate": "textured"})
    assert err.value.status == 409


def test_home_waits_for_homing_and_turns_the_nozzle_off(cc2, printer):
    result = printer.home()
    assert result == {"homed": True, "z_mm": 5.05, "nozzle_target_c": 0}
    methods = [m for m, _ in cc2.requests]
    assert methods.index(1026) < methods.index(1028)
    assert dict(cc2.requests)[1028] == {"extruder": 0}
    assert cc2.status == 1


def test_home_refuses_unless_idle(cc2, printer):
    cc2.status = 2
    with pytest.raises(PrinterError, match="not idle") as err:
        printer.home()
    assert err.value.status == 409
    assert 1026 not in [m for m, _ in cc2.requests]


def test_api_home_route(cc2, api):
    slicer, _ = api
    assert slicer.printer_call("POST", "/printer/home", {}, {})["homed"] is True
