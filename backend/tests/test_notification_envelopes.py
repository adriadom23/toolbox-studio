from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.submissions.notification_envelopes import ses_message, teams_message
from app.submissions.providers import NotificationEnvelope


def test_ses_and_teams_envelopes_only_contain_minimum_metadata():
    envelope = NotificationEnvelope("demo", "v1", "1234abcd", "alice", "submitted", "https://studio.example/review/1")
    for message in (ses_message(envelope, allowed_hosts={"studio.example"}), teams_message(envelope, allowed_hosts={"studio.example"})):
        encoded = repr(message).lower()
        assert set(message["metadata"]) == {"project", "version", "short_id", "author", "state", "review_url"}
        for forbidden in ("description", "manifest", "artifact", "secret", "path", "logs", "code"):
            assert forbidden not in encoded


def test_review_url_rejects_query_fragment_and_credentials():
    for url in (
        "https://studio.example/review?token=secret",
        "https://studio.example/review#secret",
        "https://user@studio.example/review",
    ):
        envelope = NotificationEnvelope("demo", "v1", "1234abcd", "alice", "submitted", url)
        with pytest.raises(ValueError):
            teams_message(envelope, allowed_hosts={"studio.example"})


def test_review_url_requires_allowlisted_host():
    envelope = NotificationEnvelope("demo", "v1", "1234abcd", "alice", "submitted", "https://evil.example/review")
    with pytest.raises(ValueError):
        teams_message(envelope, allowed_hosts={"studio.example"})
