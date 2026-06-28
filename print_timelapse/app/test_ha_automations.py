import json

import pytest

import ha_automations


@pytest.fixture
def _configured(monkeypatch):
    monkeypatch.setattr(ha_automations, "SUPERVISOR_TOKEN", "test-token")


@pytest.fixture
def _calls(monkeypatch, _configured):
    calls: list[tuple[str, str, dict | None]] = []

    def fake_request(method, path, body=None, base_url=ha_automations._BASE_URL):
        calls.append((method, path, body))
        return {}

    monkeypatch.setattr(ha_automations, "_request", fake_request)
    return calls


def test_not_configured_makes_no_requests(monkeypatch):
    monkeypatch.setattr(ha_automations, "SUPERVISOR_TOKEN", "")
    called = []
    monkeypatch.setattr(ha_automations, "_request", lambda *a, **k: called.append(1))
    result = ha_automations.create_automations(
        "sensor.printer_print_status", "sensor.printer_print_progress", "camera.printer", "/config/www/snap.jpg"
    )
    assert called == []
    assert result == {"configured": False, "results": {}}


def test_create_automations_posts_all_three_with_stable_ids(_calls):
    result = ha_automations.create_automations(
        "sensor.printer_print_status", "sensor.printer_print_progress", "camera.printer", "/config/www/snap.jpg"
    )
    assert result["configured"] is True
    assert result["results"] == {
        ha_automations.START_AUTOMATION_ID: True,
        ha_automations.FRAME_AUTOMATION_ID: True,
        ha_automations.FINISH_AUTOMATION_ID: True,
    }
    paths = {path for method, path, body in _calls}
    assert paths == {
        f"config/automation/config/{ha_automations.START_AUTOMATION_ID}",
        f"config/automation/config/{ha_automations.FRAME_AUTOMATION_ID}",
        f"config/automation/config/{ha_automations.FINISH_AUTOMATION_ID}",
    }
    assert all(method == "POST" for method, _, _ in _calls)


def test_one_automation_failing_is_reported_independently(monkeypatch, _configured):
    def fake_request(method, path, body=None, base_url=ha_automations._BASE_URL):
        if ha_automations.FRAME_AUTOMATION_ID in path:
            return None
        return {}

    monkeypatch.setattr(ha_automations, "_request", fake_request)
    result = ha_automations.create_automations(
        "sensor.printer_print_status", "sensor.printer_print_progress", "camera.printer", "/config/www/snap.jpg"
    )
    assert result["results"][ha_automations.START_AUTOMATION_ID] is True
    assert result["results"][ha_automations.FRAME_AUTOMATION_ID] is False
    assert result["results"][ha_automations.FINISH_AUTOMATION_ID] is True


def test_no_automation_config_uses_an_input_text_helper():
    configs = [
        ha_automations.start_automation_config("sensor.printer_print_status"),
        ha_automations.frame_automation_config(
            "sensor.printer_print_status", "sensor.printer_print_progress", "camera.printer", "/config/www/snap.jpg"
        ),
        ha_automations.finish_automation_config("sensor.printer_print_status"),
    ]
    for config in configs:
        assert "input_text" not in json.dumps(config)


def test_start_automation_derives_job_id_from_trigger_last_changed():
    config = ha_automations.start_automation_config("sensor.printer_print_status")
    assert "trigger.to_state.last_changed" in config["action"][0]["data"]["job_id"]


def test_frame_and_finish_automations_derive_job_id_from_states_last_changed():
    frame = ha_automations.frame_automation_config(
        "sensor.printer_print_status", "sensor.printer_print_progress", "camera.printer", "/config/www/snap.jpg"
    )
    finish = ha_automations.finish_automation_config("sensor.printer_print_status")
    assert "states.sensor.printer_print_status.last_changed" in frame["action"][1]["data"]["job_id"]
    assert "states.sensor.printer_print_status.last_changed" in finish["action"][0]["data"]["job_id"]


def test_detect_hostname_parses_response(monkeypatch, _configured):
    monkeypatch.setattr(ha_automations, "_request", lambda *a, **k: {"data": {"hostname": "abc123-print-timelapse"}})
    assert ha_automations.detect_hostname() == "abc123-print-timelapse"


def test_detect_hostname_returns_none_on_failure(monkeypatch, _configured):
    monkeypatch.setattr(ha_automations, "_request", lambda *a, **k: None)
    assert ha_automations.detect_hostname() is None
