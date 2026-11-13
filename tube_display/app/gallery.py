"""Ready-made screen templates (the editor's "gallery") and entity-based suggestions.

A template is {"id", "name", "description", "inputs", "screen"}. `inputs` lists the
entity ids the user must (or may) fill in: {"key", "label", "domain", optional
"device_class", optional "optional": true}. Inside the screen dict, "[[key]]"
inside any string is replaced with the chosen entity id by build().

A widget dict may carry "__optional__": "<key>" - when that input is left empty the
whole tube becomes a blank one (build() removes the marker either way).
"""

from __future__ import annotations

import copy
import re

ENTITY_ID_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
PLACEHOLDER_RE = re.compile(r"\[\[([a-z0-9_]+)\]\]")


def _states(entity: str) -> str:
    """The usual "state of an entity as a number" template for a value field."""
    return "{{ states('" + entity + "') | float(0) }}"


GALLERY: list[dict] = [
    {
        "id": "clock",
        "name": "Clock",
        "description": "The time, one big digit per tube, with seconds on the last tube.",
        "inputs": [],
        "screen": {
            "background": "#000000",
            "tubes": [
                {"type": "big_number", "value": "{{ now().strftime('%H:%M')[0] }}"},
                {"type": "big_number", "value": "{{ now().strftime('%H:%M')[1] }}"},
                {"type": "text", "text": ":", "color": "#D97757"},
                {"type": "big_number", "value": "{{ now().strftime('%H:%M')[2] }}"},
                {"type": "big_number", "value": "{{ now().strftime('%H:%M')[3] }}"},
                {"type": "big_number", "value": "{{ now().strftime('%S') }}", "label": "sec"},
            ],
        },
    },
    {
        "id": "weather",
        "name": "Weather",
        "description": "Current conditions and temperature, a graph of an outdoor "
                       "temperature sensor, and tomorrow's forecast.",
        "inputs": [
            {"key": "weather", "label": "Weather entity", "domain": "weather"},
            {"key": "temp", "label": "Outdoor temperature sensor (optional)", "domain": "sensor",
             "device_class": "temperature", "optional": True},
        ],
        "screen": {
            "background": "#000000",
            "tubes": [
                {"type": "weather", "entity": "[[weather]]", "show": "today", "label": "Now"},
                {"type": "graph", "entity": "[[temp]]", "hours": 24, "style": "area",
                 "label": "Outside", "unit": "°", "__optional__": "temp"},
                {"type": "text", "size": 22, "color": "#FFFFFF", "align": "center",
                 "text": "Tomorrow\n{{ (state_attr('[[weather]]', 'forecast') or [{}])[1].get('condition', '--') | replace('partlycloudy', 'partly cloudy') }}\n{{ (state_attr('[[weather]]', 'forecast') or [{}])[1].get('temperature', '--') }}°"},
                {"type": "text", "size": 20, "color": "#9e9e9e",
                 "text": "Wind\n{{ state_attr('[[weather]]', 'wind_speed') | round(0) }} {{ state_attr('[[weather]]', 'wind_speed_unit') or '' }}"},
                {"type": "text", "size": 20, "color": "#9e9e9e",
                 "text": "Humidity\n{{ state_attr('[[weather]]', 'humidity') | round(0) }} %"},
                {"type": "text", "size": 20, "color": "#9e9e9e",
                 "text": "Pressure\n{{ state_attr('[[weather]]', 'pressure') | round(0) }} hPa"},
            ],
        },
    },
    {
        "id": "now_playing",
        "name": "Now playing",
        "description": "What a media player is playing: icon, scrolling artist and title, "
                       "progress with the finish time, and the cover picture.",
        "inputs": [
            {"key": "player", "label": "Media player", "domain": "media_player"},
        ],
        "screen": {
            "background": "#000000",
            "tubes": [
                {"type": "icon", "icon": "mdi:music", "color": "#D97757",
                 "text": "{{ state_attr('[[player]]', 'media_artist') or '' }}"},
                {"type": "ticker", "size": 26, "speed": 40,
                 "text": "{{ state_attr('[[player]]', 'media_artist') }} – {{ state_attr('[[player]]', 'media_title') }}",
                 "label": "Now playing"},
                {"type": "progress", "value": "{{ state_attr('[[player]]', 'media_position') | float(0) }}",
                 "max": "{{ state_attr('[[player]]', 'media_duration') | float(0) }}",
                 "finish": "{{ (as_timestamp(now()) + (state_attr('[[player]]', 'media_duration') | float(0) - state_attr('[[player]]', 'media_position') | float(0))) | int }}",
                 "eta_format": "time", "label": "Playing"},
                {"type": "image", "source": "entity_picture:[[player]]", "fit": "cover"},
                {"type": "text", "size": 18, "color": "#9e9e9e",
                 "text": "{{ state_attr('[[player]]', 'media_album_name') or '' }}"},
                {"type": "text", "size": 18, "color": "#9e9e9e",
                 "text": "{{ states('[[player]]') }}"},
            ],
        },
    },
    {
        "id": "printer",
        "name": "3D printer",
        "description": "Print progress with the finish time, plus nozzle and bed temperatures.",
        "inputs": [
            {"key": "progress", "label": "Progress sensor (%)", "domain": "sensor"},
            {"key": "finish", "label": "Finish time sensor", "domain": "sensor"},
            {"key": "start", "label": "Start time sensor (optional)", "domain": "sensor",
             "optional": True},
            {"key": "nozzle", "label": "Nozzle temperature sensor (optional)", "domain": "sensor",
             "device_class": "temperature", "optional": True},
            {"key": "bed", "label": "Bed temperature sensor (optional)", "domain": "sensor",
             "device_class": "temperature", "optional": True},
        ],
        "screen": {
            "background": "#000000",
            "tubes": [
                {"type": "progress", "value": _states("[[progress]]"), "max": 100,
                 "finish": "{{ states('[[finish]]') }}", "eta_format": "time", "label": "Print"},
                {"type": "text", "size": 24, "color": "#FF9800",
                 "text": "Nozzle\n{{ states('[[nozzle]]') | float(0) | round(0) }}°",
                 "__optional__": "nozzle"},
                {"type": "text", "size": 24, "color": "#F44336",
                 "text": "Bed\n{{ states('[[bed]]') | float(0) | round(0) }}°",
                 "__optional__": "bed"},
                {"type": "text", "size": 20, "color": "#9e9e9e",
                 "text": "Elapsed\n{{ now() - as_datetime(states('[[start]]')) if states('[[start]]') not in ('unknown', 'unavailable', '') else '--' }}",
                 "__optional__": "start"},
                {"type": "blank"},
                {"type": "blank"},
            ],
        },
    },
    {
        "id": "energy",
        "name": "Energy now",
        "description": "Current power as a ring, the last 24 h as a graph, and today's energy.",
        "inputs": [
            {"key": "power", "label": "Power sensor (W)", "domain": "sensor", "device_class": "power"},
            {"key": "energy", "label": "Energy today sensor (kWh)", "domain": "sensor",
             "device_class": "energy"},
        ],
        "screen": {
            "background": "#000000",
            "tubes": [
                {"type": "ring", "value": _states("[[power]]"), "max": 4000, "unit": "W",
                 "label": "Now", "color": "#FFC107"},
                {"type": "graph", "entity": "[[power]]", "hours": 24, "style": "area",
                 "label": "Power 24 h", "unit": "W", "color": "#FFC107"},
                {"type": "big_number", "value": _states("[[energy]]"), "unit": " kWh",
                 "label": "Today", "color": "#4CAF50"},
                {"type": "text", "size": 20, "color": "#9e9e9e",
                 "text": "This hour\n{{ states('[[power]]') | float(0) | round(0) }} Wh"},
                {"type": "blank"},
                {"type": "blank"},
            ],
        },
    },
    {
        "id": "next_event",
        "name": "Next event",
        "description": "The next calendar event, scrolling, with its start time.",
        "inputs": [
            {"key": "calendar", "label": "Calendar", "domain": "calendar"},
        ],
        "screen": {
            "background": "#000000",
            "tubes": [
                {"type": "icon", "icon": "mdi:calendar-clock", "color": "#03a9f4"},
                {"type": "ticker", "size": 26, "speed": 40, "label": "Next",
                 "text": "{{ state_attr('[[calendar]]', 'message') }} — {{ as_timestamp(state_attr('[[calendar]]', 'start_time')) | timestamp_custom('%a %H:%M') }}"},
                {"type": "text", "size": 20, "color": "#9e9e9e",
                 "text": "ends\n{{ as_timestamp(state_attr('[[calendar]]', 'end_time')) | timestamp_custom('%a %H:%M') }}"},
                {"type": "text", "size": 20, "color": "#9e9e9e",
                 "text": "starts\n{{ as_timestamp(state_attr('[[calendar]]', 'start_time')) | timestamp_custom('%H:%M') }}"},
                {"type": "text", "size": 20, "color": "#9e9e9e",
                 "text": "today\n{{ now().strftime('%a %d.%m.') }}"},
                {"type": "blank"},
            ],
        },
    },
    {
        "id": "doorbell",
        "name": "Doorbell camera",
        "description": "The camera's picture across all six tubes.",
        "inputs": [
            {"key": "camera", "label": "Camera", "domain": "camera"},
        ],
        "screen": {
            "background": "#000000",
            "strip": {"source": "camera:[[camera]]", "fit": "cover"},
        },
    },
    {
        "id": "analog_clock",
        "name": "Big analogue clock",
        "description": "An analogue clock face on the first tube, the time as big digits, "
                       "the date and the seconds.",
        "inputs": [],
        "screen": {
            "background": "#000000",
            "tubes": [
                {"type": "analog_clock", "seconds": True, "numerals": True},
                {"type": "big_number", "value": "{{ now().strftime('%H') }}"},
                {"type": "text", "text": ":", "color": "#D97757"},
                {"type": "big_number", "value": "{{ now().strftime('%M') }}"},
                {"type": "text", "size": 26, "color": "#9e9e9e",
                 "text": "{{ now().strftime('%a') }}\n{{ now().strftime('%d.%m.') }}"},
                {"type": "big_number", "value": "{{ now().strftime('%S') }}", "label": "sec"},
            ],
        },
    },
]


def get_template(template_id: str) -> dict:
    for t in GALLERY:
        if t["id"] == template_id:
            return t
    raise KeyError(template_id)


def list_templates() -> list[dict]:
    """The gallery without the screen bodies: [{id, name, description, inputs}]."""
    return [{"id": t["id"], "name": t["name"], "description": t["description"],
             "inputs": [dict(i) for i in t["inputs"]]} for t in GALLERY]


def _walk_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_strings(v)


def _placeholders(obj) -> set[str]:
    found = set()
    for s in _walk_strings(obj):
        found.update(PLACEHOLDER_RE.findall(s))
    return found


def build(template_id: str, values: dict) -> dict:
    """A screen dict from a template with every "[[key]]" replaced by values[key].

    Raises ValueError for an unknown template, a missing required input, an input
    that isn't a valid entity id, or a placeholder the template doesn't declare."""
    try:
        template = get_template(template_id)
    except KeyError:
        raise ValueError(f"unknown gallery template {template_id!r}") from None
    values = values if isinstance(values, dict) else {}
    inputs = {i["key"]: i for i in template["inputs"]}
    # every placeholder must be a declared input
    for ph in _placeholders(template["screen"]):
        if ph not in inputs:
            raise ValueError(f"template {template_id!r} uses undeclared placeholder [[{ph}]]")
    resolved: dict[str, str] = {}
    for key, inp in inputs.items():
        raw = values.get(key)
        raw = "" if raw is None else str(raw).strip()
        if not raw:
            if inp.get("optional"):
                resolved[key] = ""
                continue
            raise ValueError(f"missing input '{key}' ({inp.get('label') or key})")
        if not ENTITY_ID_RE.match(raw):
            raise ValueError(f"'{raw}' is not a valid entity id (expected e.g. sensor.name)")
        resolved[key] = raw

    def sub(obj):
        if isinstance(obj, str):
            def repl(m):
                return resolved[m.group(1)]
            return PLACEHOLDER_RE.sub(repl, obj)
        if isinstance(obj, dict):
            return {k: sub(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [sub(v) for v in obj]
        return obj

    screen = sub(copy.deepcopy(template["screen"]))
    # optional tubes left empty become blank ones; the marker never reaches the file
    def drop_optional(widgets):
        out = []
        for w in widgets:
            if isinstance(w, dict) and "__optional__" in w:
                key = w.pop("__optional__")
                if not resolved.get(key):
                    out.append({"type": "blank"})
                    continue
            out.append(w)
        return out

    if isinstance(screen.get("tubes"), list):
        screen["tubes"] = drop_optional(screen["tubes"])
    return screen


# ---------------------------------------------------------------------------
# suggestions from the HA entity list (the setup wizard's step 2)
# ---------------------------------------------------------------------------

def _domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0] if "." in entity_id else ""


def _attr(state: dict, name: str):
    attrs = state.get("attributes")
    return attrs.get(name) if isinstance(attrs, dict) else None


def _first(states: list, domain: str, device_class: str | None = None,
           contains: tuple[str, ...] = ()) -> str | None:
    for s in states:
        eid = s.get("entity_id", "")
        if _domain(eid) != domain:
            continue
        if device_class and _attr(s, "device_class") != device_class:
            continue
        if contains and not all(c in eid for c in contains):
            continue
        return eid
    return None


def suggest(states: list) -> list[dict]:
    """Gallery templates that can be filled automatically from HA's /api/states.

    Returns [{"id", "name", "values", "why"}] - `values` maps input keys to entity
    ids (only inputs that were found; the user can change them in the wizard)."""
    states = states if isinstance(states, list) else []
    out: list[dict] = [{"id": "clock", "name": "Clock", "values": {},
                        "why": "no entities needed"}]

    weather = _first(states, "weather")
    if weather:
        values = {"weather": weather}
        temp = _first(states, "sensor", "temperature")
        if temp:
            values["temp"] = temp
        out.append({"id": "weather", "name": "Weather", "values": values,
                    "why": weather + (f" + {temp}" if temp else "")})

    players = [s.get("entity_id", "") for s in states
               if _domain(s.get("entity_id", "")) == "media_player"]
    for eid in players[:2]:
        out.append({"id": "now_playing", "name": "Now playing", "values": {"player": eid},
                    "why": eid})

    power = _first(states, "sensor", "power")
    if power:
        values = {"power": power}
        energy = _first(states, "sensor", "energy")
        if energy:
            values["energy"] = energy
        out.append({"id": "energy", "name": "Energy now", "values": values,
                    "why": power + (f" + {energy}" if energy else "")})

    progress = _first(states, "sensor", contains=("print", "progress"))
    if progress:
        values = {"progress": progress}
        finish = _first(states, "sensor", contains=("print",))
        for cand in states:
            eid = cand.get("entity_id", "")
            if _domain(eid) == "sensor" and "print" in eid and any(
                    w in eid for w in ("finish", "end", "eta", "remaining")):
                finish = eid
                break
        if finish:
            values["finish"] = finish
        nozzle = _first(states, "sensor", contains=("nozzle",))
        bed = _first(states, "sensor", contains=("bed",))
        if nozzle:
            values["nozzle"] = nozzle
        if bed:
            values["bed"] = bed
        out.append({"id": "printer", "name": "3D printer", "values": values,
                    "why": ", ".join(values.values())})

    calendar = _first(states, "calendar")
    if calendar:
        out.append({"id": "next_event", "name": "Next event", "values": {"calendar": calendar},
                    "why": calendar})

    camera = _first(states, "camera")
    if camera:
        out.append({"id": "doorbell", "name": "Doorbell camera", "values": {"camera": camera},
                    "why": camera})
    return out
