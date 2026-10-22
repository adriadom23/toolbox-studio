"""Unit tests for ``app.api.browser_routes`` (the ``/api/browser/open-firefox``
route that ``shared/openLoginTab.js`` / ``shared/api/http.js``'s
``openInFirefox()`` call).

Never spawns a real Firefox process: ``resolve_firefox_binary`` and
``subprocess.Popen`` are monkeypatched in every test.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from flask import Flask

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.api.browser_routes import browser_bp  # noqa: E402


@pytest.fixture()
def client():
    app = Flask(__name__)
    app.register_blueprint(browser_bp, url_prefix="/api/browser")
    return app.test_client()


def test_open_firefox_rejects_non_http_url(client):
    resp = client.post("/api/browser/open-firefox", json={"url": "file:///etc/passwd"})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "invalid_url"


def test_open_firefox_rejects_missing_url(client):
    resp = client.post("/api/browser/open-firefox", json={})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "invalid_url"


def test_open_firefox_reports_not_found_without_raising(client, monkeypatch):
    import vendor.integrations.selenium_client as selenium_client

    monkeypatch.setattr(selenium_client, "resolve_firefox_binary", lambda: None)

    resp = client.post(
        "/api/browser/open-firefox", json={"url": "https://example.amazon.com/login"}
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body == {"ok": True, "opened": False, "error": "firefox_not_found"}


def test_open_firefox_spawns_detached_process_and_returns_opened(client, monkeypatch):
    import vendor.integrations.selenium_client as selenium_client
    import app.api.browser_routes as browser_routes

    monkeypatch.setattr(
        selenium_client, "resolve_firefox_binary", lambda: r"C:\Firefox\firefox.exe"
    )

    captured = {}

    class _FakePopen:
        def __init__(self, args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

    monkeypatch.setattr(browser_routes.subprocess, "Popen", _FakePopen)

    resp = client.post(
        "/api/browser/open-firefox", json={"url": "https://example.amazon.com/login"}
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "opened": True}
    assert captured["args"] == [
        r"C:\Firefox\firefox.exe",
        "-new-tab",
        "https://example.amazon.com/login",
    ]


def test_open_firefox_handles_spawn_failure(client, monkeypatch):
    import vendor.integrations.selenium_client as selenium_client
    import app.api.browser_routes as browser_routes

    monkeypatch.setattr(
        selenium_client, "resolve_firefox_binary", lambda: r"C:\Firefox\firefox.exe"
    )

    def _raise(*args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(browser_routes.subprocess, "Popen", _raise)

    resp = client.post(
        "/api/browser/open-firefox", json={"url": "https://example.amazon.com/login"}
    )
    assert resp.status_code == 500
    body = resp.get_json()
    assert body["ok"] is False
    assert body["opened"] is False
    assert "boom" in body["error"]
