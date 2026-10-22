"""Unit tests for ``app.core.owner_config`` (design.md §3.2).

Covers: (a) no config file present -> the two-login default, (b) a config
file present -> its contents, (c) ``STUDIO_OWNER_EMAILS``/
``STUDIO_SLACK_WEBHOOK`` env override the JSON file's values.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core import owner_config  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("STUDIO_OWNER_EMAILS", raising=False)
    monkeypatch.delenv("STUDIO_SLACK_WEBHOOK", raising=False)
    monkeypatch.delenv("STUDIO_CONFIG_PATH", raising=False)


def test_no_config_file_returns_two_login_default(tmp_path, monkeypatch):
    missing_path = tmp_path / "studio.config.json"
    monkeypatch.setenv("STUDIO_CONFIG_PATH", str(missing_path))

    config = owner_config.load_owner_config()

    assert config["owners"] == list(owner_config.DEFAULT_OWNERS)
    assert owner_config.owners() == list(owner_config.DEFAULT_OWNERS)


def test_config_file_present_returns_its_contents(tmp_path, monkeypatch):
    config_path = tmp_path / "studio.config.json"
    custom = {
        "owners": ["someone"],
        "notifications": {
            "email": {"enabled": True, "to": ["someone@amazon.com"], "transport": "smtp"},
            "slack": {"enabled": True, "webhook_url": "https://hooks.slack.com/services/TEST/TEST/TEST"},
        },
    }
    config_path.write_text(json.dumps(custom), encoding="utf-8")
    monkeypatch.setenv("STUDIO_CONFIG_PATH", str(config_path))

    config = owner_config.load_owner_config()
    assert config == custom
    assert owner_config.owners() == ["someone"]

    targets = owner_config.notification_targets()
    assert targets["emails"] == ["someone@amazon.com"]
    assert targets["slack_webhook"] == "https://hooks.slack.com/services/TEST/TEST/TEST"


def test_env_var_overrides_json_file_email_list(tmp_path, monkeypatch):
    config_path = tmp_path / "studio.config.json"
    custom = {
        "owners": ["someone"],
        "notifications": {
            "email": {"enabled": True, "to": ["someone@amazon.com"], "transport": "smtp"},
            "slack": {"enabled": False, "webhook_url": ""},
        },
    }
    config_path.write_text(json.dumps(custom), encoding="utf-8")
    monkeypatch.setenv("STUDIO_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("STUDIO_OWNER_EMAILS", "a@amazon.com, b@amazon.com")
    monkeypatch.setenv("STUDIO_SLACK_WEBHOOK", "https://hooks.slack.com/services/TEST/TEST/TEST")

    targets = owner_config.notification_targets()

    assert targets["emails"] == ["a@amazon.com", "b@amazon.com"]
    assert targets["slack_webhook"] == "https://hooks.slack.com/services/TEST/TEST/TEST"


def test_malformed_config_file_falls_back_to_default(tmp_path, monkeypatch):
    config_path = tmp_path / "studio.config.json"
    config_path.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setenv("STUDIO_CONFIG_PATH", str(config_path))

    config = owner_config.load_owner_config()

    assert config["owners"] == list(owner_config.DEFAULT_OWNERS)
