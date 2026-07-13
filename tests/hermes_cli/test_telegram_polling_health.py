"""Dashboard projection of the sanitized Telegram polling heartbeat."""

import pytest


@pytest.mark.parametrize("poll_state", ["healthy", "stopped", "webhook"])
def test_messaging_payload_surfaces_telegram_poll_health(monkeypatch, poll_state):
    from hermes_cli import web_server as ws

    entry = {
        "id": "telegram",
        "name": "Telegram",
        "description": "Telegram bot",
        "docs_url": "",
        "env_vars": ("TELEGRAM_BOT_TOKEN",),
        "required_env": ("TELEGRAM_BOT_TOKEN",),
    }
    monkeypatch.setattr(ws, "get_running_pid", lambda: None)
    monkeypatch.setattr(ws, "get_runtime_status_running_pid", lambda runtime: None)
    monkeypatch.setattr(
        ws,
        "load_config",
        lambda: {"platforms": {"telegram": {"enabled": True}}},
    )

    payload = ws._messaging_platform_payload(
        entry,
        {"TELEGRAM_BOT_TOKEN": "test-only-token"},
        runtime={
            "platforms": {
                "telegram": {
                    "state": "connected",
                    "poll_state": poll_state,
                    "poll_last_success_at": "2026-07-14T00:00:25+00:00",
                    "poll_stale_after_seconds": 180.0,
                }
            }
        },
        scoped=True,
    )

    assert payload["poll_state"] == poll_state
    assert payload["poll_last_success_at"] == "2026-07-14T00:00:25+00:00"
    assert payload["poll_stale_after_seconds"] == 180.0
    assert "test-only-token" not in str(payload)
