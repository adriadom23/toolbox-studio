from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from scripts.smoke_submissions import run_smoke


def test_real_http_submission_smoke(tmp_path, monkeypatch):
    conflicting = tmp_path / "private-default.json"
    conflicting.write_text(json.dumps({
        "schema_version": 2,
        "server": {"profile": "shared", "bind": "0.0.0.0", "public_base_url": "https://studio.invalid", "cors_origins": ["https://studio.invalid"]},
        "preview": {"origin": "https://preview.invalid", "allowed_origins": ["https://preview.invalid"]},
    }), encoding="utf-8")
    monkeypatch.setenv("STUDIO_CONFIG_PATH", str(conflicting))
    result = run_smoke(tmp_path / "smoke-runtime")
    assert result["status"] == "published"
    assert len(result["artifact_sha256"]) == 64
    assert result["boto3_loaded"] is False
