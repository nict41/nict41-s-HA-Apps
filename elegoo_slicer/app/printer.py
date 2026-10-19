"""Talking to an Elegoo Centauri Carbon 2: upload, list, delete, start.

The CC2 has two doors. Files go in over HTTP (PUT /upload on port 80, raw
1 MiB chunks, each carrying the whole file's MD5 and the access code).
Everything else is JSON-RPC over MQTT on port 1883, against a broker the
printer runs itself: a client registers, then publishes {"id", "method",
"params"} to elegoo/<sn>/<client>/api_request and reads the reply with the
same id from .../api_response.

Two behaviours shape this module:

- The printer accepts only a few registered clients and keeps a slot for
  about a minute after a client goes quiet. Home Assistant's integration holds
  one permanently and the desktop slicer takes another. So a session is opened
  per API call, kept to the few requests that call needs, and closed - never
  one session per request - and "too many clients" is reported as such rather
  than as a timeout.
- Starting a print is the one irreversible thing here. It is only sent when
  the caller says confirm, the printer reports idle, the file is on the printer
  and every tray in the slot map is loaded.

Message shapes are the ones ElegooSlicer itself sent to this printer
(firmware 02.01.01.07), captured off the broker.

Standard library only; MQTT 3.1.1 needs about sixty lines of it.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import random
import socket
import struct
import time
from pathlib import Path
from typing import Any

CHUNK = 1024 * 1024
POLL_SECONDS = 2

METHOD_STATUS = 1002
METHOD_START_PRINT = 1020
METHOD_HOME = 1026
METHOD_SET_TEMPERATURE = 1028
METHOD_FILE_LIST = 1044
METHOD_FILE_DETAIL = 1046
METHOD_DELETE_FILE = 1047
METHOD_SET_AUTO_REFILL = 2004
METHOD_CANVAS = 2005

STATUS_IDLE = 1
STATUS_PRINTING = 2
STATUS_HOMING = 10
STATUS_NAMES = {
    0: "initializing", 1: "idle", 2: "printing", 3: "filament operating", 4: "filament operating",
    5: "auto leveling", 6: "PID calibrating", 7: "resonance testing", 8: "self checking",
    9: "updating", 10: "homing", 11: "file transferring", 12: "video composing",
    13: "extruder operating", 14: "emergency stop", 15: "power loss recovery",
}

# What ElegooSlicer sends in START_PRINT's config, minus the slot map.
DEFAULT_PRINT_CONFIG = {
    "bedlevel_force": False,
    "delay_video": True,
    "print_layout": "B",
    "printer_check": True,
}


class PrinterError(Exception):
    """The printer refused, could not be reached, or is not in a state to do it."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


# ── MQTT ────────────────────────────────────────────────────────────────────


def _s16(data: bytes) -> bytes:
    return struct.pack("!H", len(data)) + data


def _remaining_length(n: int) -> bytes:
    out = b""
    while True:
        digit, n = n % 128, n // 128
        out += bytes([digit | (0x80 if n else 0)])
        if not n:
            return out


def _packet(kind: int, body: bytes) -> bytes:
    return bytes([kind]) + _remaining_length(len(body)) + body


class Session:
    """One registered MQTT client, for the duration of a `with` block."""

    def __init__(self, host: str, serial: str, access_code: str, *, port: int = 1883, timeout: float = 10):
        self.host, self.port, self.serial, self.code = host, port, serial, access_code
        self.timeout = timeout
        self.client_id = f"1_PC_{random.randint(1000, 9999)}"
        self._sock: socket.socket | None = None
        self._next_id = 1
        self._buffer = b""

    # -- wire

    def _send(self, data: bytes) -> None:
        assert self._sock is not None
        self._sock.sendall(data)

    def _recv_exact(self, n: int, deadline: float) -> bytes:
        assert self._sock is not None
        while len(self._buffer) < n:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError
            self._sock.settimeout(left)
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout:
                raise TimeoutError from None
            if not chunk:
                raise PrinterError("printer closed the MQTT connection")
            self._buffer += chunk
        data, self._buffer = self._buffer[:n], self._buffer[n:]
        return data

    def _read_packet(self, deadline: float) -> tuple[int, bytes]:
        kind = self._recv_exact(1, deadline)[0]
        mult, length = 1, 0
        while True:
            byte = self._recv_exact(1, deadline)[0]
            length += (byte & 0x7F) * mult
            mult *= 128
            if not byte & 0x80:
                break
        return kind, self._recv_exact(length, deadline)

    def _publish(self, topic: str, payload: dict[str, Any]) -> None:
        self._send(_packet(0x30, _s16(topic.encode()) + json.dumps(payload).encode()))

    def _wait_for(self, topic: str, match, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            try:
                kind, body = self._read_packet(deadline)
            except TimeoutError:
                raise PrinterError(f"no reply from the printer on {topic.rsplit('/', 1)[-1]}", 504) from None
            if kind >> 4 != 3:  # only PUBLISH carries messages
                continue
            tlen = struct.unpack("!H", body[:2])[0]
            if body[2:2 + tlen].decode(errors="replace") != topic:
                continue
            try:
                message = json.loads(body[2 + tlen:])
            except ValueError:
                continue
            if match(message):
                return message

    # -- session

    def __enter__(self) -> Session:
        base = f"elegoo/{self.serial}"
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as err:
            raise PrinterError(f"cannot reach the printer at {self.host}:{self.port}: {err}", 504) from None
        connect = (_s16(b"MQTT") + bytes([4, 0xC2]) + struct.pack("!H", 60)
                   + _s16(self.client_id.encode()) + _s16(b"elegoo") + _s16(self.code.encode()))
        self._send(_packet(0x10, connect))
        kind, body = self._read_packet(time.monotonic() + self.timeout)
        if kind >> 4 != 2 or len(body) < 2 or body[1] != 0:
            self.close()
            raise PrinterError("printer refused the MQTT login (wrong access code?)", 502)
        topics = [f"{base}/{self.client_id}/api_response", f"{base}/{self.client_id}_req/register_response"]
        sub = struct.pack("!H", 1) + b"".join(_s16(t.encode()) + b"\x00" for t in topics)
        self._send(_packet(0x82, sub))
        self._publish(f"{base}/api_register", {"client_id": self.client_id, "request_id": f"{self.client_id}_req"})
        reply = self._wait_for(topics[1], lambda m: True, self.timeout)
        error = str(reply.get("error", ""))
        if error not in ("ok", "already registered"):
            self.close()
            if "too many" in error:
                raise PrinterError(
                    "printer has no free client slots (too many clients); slots free up about a "
                    "minute after a client goes quiet, so try again shortly", 503)
            raise PrinterError(f"printer refused registration: {error}", 502)
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._send(b"\xe0\x00")  # DISCONNECT
            except OSError:
                pass
            self._sock.close()
            self._sock = None

    def request(self, method: int, params: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        """Send one command and return its `result`."""
        rid = self._next_id
        self._next_id += 1
        base = f"elegoo/{self.serial}/{self.client_id}"
        self._publish(f"{base}/api_request", {"id": rid, "method": method, "params": params or {}})
        reply = self._wait_for(f"{base}/api_response",
                               lambda m: m.get("id") == rid and m.get("method") == method,
                               timeout or self.timeout)
        return reply.get("result") or {}


# ── Operations ──────────────────────────────────────────────────────────────


class Printer:
    def __init__(self, host: str, serial: str, access_code: str, *, mqtt_port: int = 1883, http_port: int = 80):
        if not (host and serial and access_code):
            raise PrinterError(
                "printer not configured: set printer_host, printer_serial and printer_access_code "
                "in the add-on options", 503)
        self.host, self.serial, self.code = host, serial, access_code
        self.mqtt_port, self.http_port = mqtt_port, http_port

    def session(self) -> Session:
        return Session(self.host, self.serial, self.code, port=self.mqtt_port)

    @staticmethod
    def _summarise(status: dict[str, Any], canvas: dict[str, Any]) -> dict[str, Any]:
        machine = status.get("machine_status") or {}
        job = status.get("print_status") or {}
        trays = []
        info = canvas.get("canvas_info") or {}
        for box in info.get("canvas_list") or []:
            for tray in box.get("tray_list") or []:
                trays.append({
                    "canvas_id": box.get("canvas_id", 0),
                    "tray_id": tray.get("tray_id"),
                    "loaded": tray.get("status") == 1,
                    "type": tray.get("filament_type"),
                    "name": tray.get("filament_name"),
                    "colour": tray.get("filament_color"),
                })
        code = machine.get("status")
        return {
            "state": STATUS_NAMES.get(code, f"unknown ({code})"),
            "status_code": code,
            "sub_status": machine.get("sub_status"),
            "idle": code == STATUS_IDLE,
            "job": {k: job.get(k) for k in ("filename", "state", "current_layer", "print_duration", "remaining_time_sec")},
            "nozzle_c": (status.get("extruder") or {}).get("temperature"),
            "bed_c": (status.get("heater_bed") or {}).get("temperature"),
            "canvas_connected": any(b.get("connected") for b in info.get("canvas_list") or []),
            "auto_refill": info.get("auto_refill"),
            "trays": trays,
        }

    def status(self) -> dict[str, Any]:
        with self.session() as s:
            return self._summarise(s.request(METHOD_STATUS), s.request(METHOD_CANVAS))

    def files(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.session() as s:
            result = s.request(METHOD_FILE_LIST, {"storage_media": "local", "offset": 0, "limit": limit})
        return result.get("file_list") or []

    def upload(self, path: Path, name: str) -> dict[str, Any]:
        data = path.read_bytes()
        total = len(data)
        digest = hashlib.md5(data).hexdigest()  # noqa: S324 - the printer's integrity check, not security
        # One connection for every chunk. The printer's HTTP server answers 408
        # to a new connection that turns up in the middle of a transfer.
        conn = http.client.HTTPConnection(self.host, self.http_port, timeout=60)
        try:
            for start in range(0, total, CHUNK):
                self._upload_chunk(conn, data, start, total, name, digest)
        finally:
            conn.close()
        # The printer parses the file once it has all of it; reading the detail
        # back proves it arrived whole and was understood.
        detail = self.file_detail(name)
        if detail.get("size") not in (None, total):
            raise PrinterError(f"printer has {detail.get('size')} bytes of {name}, sent {total}")
        return {"filename": name, "bytes": total, "md5": digest, "detail": detail}

    def _upload_chunk(self, conn: http.client.HTTPConnection, data: bytes, start: int,
                      total: int, name: str, digest: str) -> None:
        chunk = data[start:start + CHUNK]
        try:
            conn.request("PUT", "/upload", body=chunk, headers={
                "Content-Type": "application/octet-stream",
                "Content-Range": f"bytes {start}-{start + len(chunk) - 1}/{total}",
                "X-File-Name": name,
                "X-File-MD5": digest,
                "X-Token": self.code,
                "Connection": "keep-alive",
            })
            resp = conn.getresponse()
            body = resp.read().decode(errors="replace")
        except OSError as err:
            raise PrinterError(f"upload to {self.host} failed at byte {start}: {err}", 504) from None
        if resp.status >= 300:
            raise PrinterError(f"printer rejected upload chunk at byte {start}: HTTP {resp.status} {body[:200]}")
        try:
            code = json.loads(body).get("error_code", 0) if body.strip() else 0
        except ValueError:
            code = 0
        if code:
            raise PrinterError(f"printer rejected upload chunk at byte {start}: error_code {code}")

    def file_detail(self, name: str) -> dict[str, Any]:
        with self.session() as s:
            result = s.request(METHOD_FILE_DETAIL, {"storage_media": "local", "filename": name})
        if result.get("error_code"):
            raise PrinterError(f"printer has no file {name!r} (error_code {result['error_code']})", 404)
        return result

    def delete(self, name: str) -> None:
        with self.session() as s:
            result = s.request(METHOD_DELETE_FILE, {"storage_media": "local", "file_path": [name]})
        if result.get("error_code"):
            raise PrinterError(f"printer refused to delete {name!r} (error_code {result['error_code']})")

    def home(self, *, timeout: float = 180) -> dict[str, Any]:
        """Home all axes, which also raises the bed to the nozzle.

        That is what puts the bed in the chamber camera's view for a check
        before a print. Homing heats the nozzle to 140 °C for probing and
        leaves it there, so the heater target is put back to 0 afterwards.
        """
        with self.session() as s:
            machine = s.request(METHOD_STATUS).get("machine_status") or {}
            if machine.get("status") != STATUS_IDLE:
                raise PrinterError(f"printer is {STATUS_NAMES.get(machine.get('status'))}, not idle", 409)
            result = s.request(METHOD_HOME, {})
            if result.get("error_code"):
                raise PrinterError(f"printer refused to home (error_code {result['error_code']})")
            started = time.monotonic()
            deadline = started + timeout
            began = False
            while True:
                time.sleep(POLL_SECONDS)
                status = s.request(METHOD_STATUS)
                code = (status.get("machine_status") or {}).get("status")
                began = began or code == STATUS_HOMING
                # Done once it has been homing and is idle again. A home so
                # quick that no poll saw it counts as done after ten seconds.
                if code == STATUS_IDLE and (began or time.monotonic() - started > 10):
                    break
                if time.monotonic() > deadline:
                    raise PrinterError(f"printer still {STATUS_NAMES.get(code)} {timeout:.0f}s after home", 504)
            s.request(METHOD_SET_TEMPERATURE, {"extruder": 0})
            status = s.request(METHOD_STATUS)
        move = status.get("gcode_move") or {}
        return {
            "homed": True,
            "z_mm": move.get("z"),
            "nozzle_target_c": (status.get("extruder") or {}).get("target"),
        }

    def start(
        self,
        name: str,
        slot_map: list[dict[str, Any]],
        *,
        auto_refill: bool | None = True,
        config: dict[str, Any] | None = None,
        verify_seconds: float = 20,
    ) -> dict[str, Any]:
        """Start printing a file already on the printer, after checking it can."""
        with self.session() as s:
            status = self._summarise(s.request(METHOD_STATUS), s.request(METHOD_CANVAS))
            if not status["idle"]:
                raise PrinterError(f"printer is {status['state']}, not idle", 409)
            detail = s.request(METHOD_FILE_DETAIL, {"storage_media": "local", "filename": name})
            if detail.get("error_code"):
                raise PrinterError(f"printer has no file {name!r}", 404)

            used = sorted({int(c.get("t", 0)) for c in detail.get("color_map") or []}) or [0]
            mapped = {int(m["t"]) for m in slot_map}
            if missing := [t for t in used if t not in mapped]:
                raise PrinterError(f"slot_map has no tray for filament(s) {missing} used by {name}", 400)
            if status["canvas_connected"]:
                trays = {(t["canvas_id"], t["tray_id"]): t for t in status["trays"]}
                for m in slot_map:
                    tray = trays.get((int(m.get("canvas_id", 0)), int(m["tray_id"])))
                    if tray is None:
                        raise PrinterError(f"no CANVAS tray {m['tray_id']}", 400)
                    if not tray["loaded"]:
                        raise PrinterError(f"CANVAS tray {m['tray_id']} is empty", 409)

            if auto_refill is not None:
                s.request(METHOD_SET_AUTO_REFILL, {"auto_refill": bool(auto_refill)})
            params = {
                "config": {**DEFAULT_PRINT_CONFIG, **(config or {}), "slot_map": [
                    {"canvas_id": int(m.get("canvas_id", 0)), "t": int(m["t"]), "tray_id": int(m["tray_id"])}
                    for m in slot_map
                ]},
                "filename": name,
                "storage_media": "local",
            }
            result = s.request(METHOD_START_PRINT, params)
            if result.get("error_code"):
                raise PrinterError(f"printer refused to start {name!r} (error_code {result['error_code']})")

            # Accepted is not started: wait until the printer says it is printing.
            deadline = time.monotonic() + verify_seconds
            while True:
                machine = (s.request(METHOD_STATUS).get("machine_status") or {})
                if machine.get("status") == STATUS_PRINTING:
                    return {"started": True, "filename": name, "sent": params, "sub_status": machine.get("sub_status")}
                if time.monotonic() > deadline:
                    raise PrinterError(
                        f"printer accepted the start but is still {STATUS_NAMES.get(machine.get('status'))} "
                        f"after {verify_seconds:.0f}s", 504)
                time.sleep(POLL_SECONDS)
