"""Creates the three Home Assistant automations this add-on depends on,
using the add-on's own Supervisor-granted access to the Home Assistant Core
API (`homeassistant_api: true` in config.yaml) - the same REST endpoint
Home Assistant's own Automation Editor uses to save an automation
(`POST /config/automation/config/<automation_id>`).

Each automation is saved under a stable, well-known id, so re-running this
(e.g. after changing an entity id on the Help page) updates the existing
automation in place rather than creating a duplicate.

There's deliberately no `input_text` helper here: helpers are config-entry
-backed and only creatable through Home Assistant's websocket config-flow
API, which has no precedent in this codebase (everything here is plain
synchronous `urllib`). Instead, each automation derives the shared job_id
from the print-status sensor's own `last_changed` timestamp - stable for
the whole "printing" episode, since it only moves on a state-*value*
change, and behaviorally equivalent to a helper holding the same value.

Hostname is never threaded into any of this: the automations call
rest_commands by their Home Assistant service/action name
(`rest_command.timelapse_start`, etc.), not by URL - hostname only matters
for the `rest_command:` YAML text itself, which is generated client-side in
help.html and never touches this module.
"""

import json
import os
import urllib.error
import urllib.request

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "").strip()

_BASE_URL = "http://supervisor/core/api"
_HASSIO_BASE_URL = "http://supervisor/addons/self"
_DEFAULT_HOSTNAME = "beb500c8-print-timelapse"

START_AUTOMATION_ID = "print_timelapse_start"
FRAME_AUTOMATION_ID = "print_timelapse_capture_frame"
FINISH_AUTOMATION_ID = "print_timelapse_finish"


def configured() -> bool:
    return bool(SUPERVISOR_TOKEN)


def _request(method: str, path: str, body: dict | None = None, base_url: str = _BASE_URL) -> dict | None:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"{base_url}/{path}",
        data=data,
        method=method,
        headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"[print_timelapse] Home Assistant API request to '{path}' failed: {exc}")
        return None
    except ValueError as exc:
        print(f"[print_timelapse] Home Assistant API response for '{path}' wasn't valid JSON: {exc}")
        return None


def _put_automation(automation_id: str, config: dict) -> bool:
    return _request("POST", f"config/automation/config/{automation_id}", config) is not None


def start_automation_config(print_status_entity: str) -> dict:
    return {
        "alias": "Timelapse: start",
        "trigger": [{"trigger": "state", "entity_id": print_status_entity, "to": "printing"}],
        "action": [
            {
                "action": "rest_command.timelapse_start",
                "data": {"job_id": "{{ trigger.to_state.last_changed.strftime('%Y%m%d_%H%M%S') }}"},
            }
        ],
    }


def frame_automation_config(
    print_status_entity: str,
    print_progress_entity: str,
    snapshot_camera_entity: str,
    snapshot_filename: str,
) -> dict:
    return {
        "alias": "Timelapse: capture frame",
        "trigger": [{"trigger": "state", "entity_id": print_progress_entity}],
        "condition": [
            {
                "condition": "template",
                "value_template": "{{ trigger.to_state.state | int(0) != trigger.from_state.state | int(0) }}",
            }
        ],
        "action": [
            {
                "action": "camera.snapshot",
                "target": {"entity_id": snapshot_camera_entity},
                "data": {"filename": snapshot_filename},
            },
            {
                "action": "rest_command.timelapse_frame",
                "data": {
                    "job_id": f"{{{{ states.{print_status_entity}.last_changed.strftime('%Y%m%d_%H%M%S') }}}}",
                    "percent": "{{ trigger.to_state.state | int }}",
                },
            },
        ],
    }


def finish_automation_config(print_status_entity: str) -> dict:
    return {
        "alias": "Timelapse: finish",
        "trigger": [{"trigger": "state", "entity_id": print_status_entity, "to": "completed"}],
        "action": [
            {
                "action": "rest_command.timelapse_finish",
                "data": {"job_id": f"{{{{ states.{print_status_entity}.last_changed.strftime('%Y%m%d_%H%M%S') }}}}"},
            }
        ],
    }


def create_automations(
    print_status_entity: str,
    print_progress_entity: str,
    snapshot_camera_entity: str,
    snapshot_filename: str,
) -> dict:
    """Creates/updates all 3 automations. Returns
    {"configured": bool, "results": {automation_id: bool}} - per-automation
    success/failure, so one failing doesn't hide that the others went through."""
    if not configured():
        return {"configured": False, "results": {}}
    return {
        "configured": True,
        "results": {
            START_AUTOMATION_ID: _put_automation(START_AUTOMATION_ID, start_automation_config(print_status_entity)),
            FRAME_AUTOMATION_ID: _put_automation(
                FRAME_AUTOMATION_ID,
                frame_automation_config(print_status_entity, print_progress_entity, snapshot_camera_entity, snapshot_filename),
            ),
            FINISH_AUTOMATION_ID: _put_automation(FINISH_AUTOMATION_ID, finish_automation_config(print_status_entity)),
        },
    }


def detect_hostname() -> str | None:
    """Best-effort hostname self-detection via hassio_api; None on any
    failure - callers fall back to _DEFAULT_HOSTNAME."""
    info = _request("GET", "info", base_url=_HASSIO_BASE_URL)
    hostname = (info or {}).get("data", {}).get("hostname")
    return hostname if isinstance(hostname, str) and hostname else None
