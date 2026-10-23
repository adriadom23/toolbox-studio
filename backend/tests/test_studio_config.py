from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.core.studio_config import load_studio_config, map_identity  # noqa: E402


def test_local_defaults_are_loopback_and_need_no_aws(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_CONFIG_PATH", str(tmp_path / "missing.json"))
    config = load_studio_config()
    assert config.profile == "local"
    assert config.host == "127.0.0.1"
    assert config.local_user_id == "local-user"
    assert config.local_display_name == "Propietario local"
    assert config.identity_roles["local-user"] == ("admin", "reviewer", "submitter")
    assert config.proxy_hmac_secret is None


def test_local_actor_can_be_configured_and_environment_overrides_are_validated(tmp_path, monkeypatch):
    path = tmp_path / "identity.json"
    path.write_text(json.dumps({
        "schema_version": 2,
        "identity": {
            "user_id": "local-owner",
            "display_name": "Propietaria local",
            "roles": {"local-owner": ["admin", "submitter"]},
        },
    }), encoding="utf-8")
    configured = load_studio_config(path)
    assert configured.local_user_id == "local-owner"
    assert configured.local_display_name == "Propietaria local"
    assert configured.identity_roles["local-owner"] == ("admin", "submitter")

    monkeypatch.setenv("STUDIO_LOCAL_USER_ID", "environment-owner")
    monkeypatch.setenv("STUDIO_LOCAL_DISPLAY_NAME", "Propietario del entorno")
    overridden = load_studio_config(path)
    assert overridden.local_user_id == "environment-owner"
    assert overridden.local_display_name == "Propietario del entorno"
    assert overridden.identity_roles["environment-owner"] == ("admin", "submitter")
    assert "local-owner" not in overridden.identity_roles

    monkeypatch.setenv("STUDIO_LOCAL_USER_ID", " owner ")
    with pytest.raises(ValueError, match="identity.user_id"):
        load_studio_config(path)
    monkeypatch.setenv("STUDIO_LOCAL_USER_ID", "environment-owner")
    monkeypatch.setenv("STUDIO_LOCAL_DISPLAY_NAME", "a" * 201)
    with pytest.raises(ValueError, match="identity.display_name"):
        load_studio_config(path)


def test_relative_runtime_and_preview_are_resolved_from_repository_config(tmp_path):
    path = BACKEND_DIR / "config" / "studio.config.example.json"
    config = load_studio_config(path)
    assert config.database_path == BACKEND_DIR / "data" / "runtime" / "submissions.sqlite3"
    assert config.preview_origin == "http://127.0.0.1:5001"
    assert config.preview_host == "127.0.0.1"
    assert config.preview_port == 5001


def test_local_rejects_non_loopback(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"schema_version": 2, "server": {"profile": "local", "bind": "0.0.0.0"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="loopback"):
        load_studio_config(path)


def test_preview_origin_is_separate_coherent_and_allowlisted(tmp_path, monkeypatch):
    path = tmp_path / "preview.json"
    invalid_local = (
        {"bind": "127.0.0.1", "origin": "http://example.invalid:5001"},
        {"bind": "localhost", "origin": "http://127.0.0.1:5001"},
        {"bind": "127.0.0.1", "origin": "http://127.0.0.1:5173"},
    )
    for preview in invalid_local:
        path.write_text(json.dumps({"schema_version": 2, "preview": preview}), encoding="utf-8")
        with pytest.raises(ValueError, match="config_invalid"):
            load_studio_config(path)

    monkeypatch.setenv("SHARED_RUNTIME_TOKEN", "test-only")
    shared = {
        "schema_version": 2,
        "server": {"profile": "shared", "bind": "0.0.0.0", "public_base_url": "https://studio.example", "cors_origins": ["https://studio.example"]},
        "identity": {"proxy_hmac_secret_env": "STUDIO_PROXY_HMAC_SECRET", "trusted_peer_cidrs": ["127.0.0.1/32"]},
        "providers": {"artifact": "s3", "build": "codebuild", "runtime": "isolated-gateway"},
        "storage": {"s3_bucket": "private"},
        "build": {
            "codebuild_project": "toolbox",
            "network_preflight": "aws-vpc-endpoints-v1",
            "approved_image_digest": "a" * 64,
            "approved_wrapper_sha256": "b" * 64,
            "probe_version": "probe-v1",
            "account_id": "111122223333",
            "region": "eu-west-1",
        },
        "runtime": {"gateway_url": "https://runtime.example", "gateway_token_env": "SHARED_RUNTIME_TOKEN", "gateway_allowed_hosts": ["runtime.example"]},
        "preview": {"bind": "0.0.0.0", "origin": "https://preview.example"},
    }
    monkeypatch.setenv("STUDIO_PROXY_HMAC_SECRET", "test-only")
    path.write_text(json.dumps(shared), encoding="utf-8")
    with pytest.raises(ValueError, match="explicitly allowlisted"):
        load_studio_config(path)
    shared["preview"]["allowed_origins"] = ["https://preview.example"]
    path.write_text(json.dumps(shared), encoding="utf-8")
    assert load_studio_config(path).preview_origin == "https://preview.example"


def test_exact_legacy_v1_and_v2_mapping(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"owners": ["owner"], "notifications": {}}), encoding="utf-8")
    assert load_studio_config(path).profile == "local"
    path.write_text(json.dumps({"schema_version": 2, "identity": {"mapping_version": 1, "mapping": {"proxy-user": "reviewer"}}}), encoding="utf-8")
    config = load_studio_config(path)
    assert map_identity(config, "proxy-user") == "reviewer"
    path.write_text(json.dumps({"schema_version": 2, "identity": {"mapping_version": 2}}), encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported"):
        load_studio_config(path)


def test_identity_mapping_keys_values_and_role_actors_are_strict(tmp_path):
    path = tmp_path / "identity.json"
    invalid_identity_sections = (
        {"mapping": {"": "alice"}},
        {"mapping": {"alice": "a" * 201}},
        {"mapping": {"alice\nadmin": "alice"}},
        {"mapping": {"ａｌｉｃｅ": "alice"}},
        {"roles": {" alice ": ["submitter"]}},
    )
    for identity in invalid_identity_sections:
        path.write_text(json.dumps({"schema_version": 2, "identity": identity}), encoding="utf-8")
        with pytest.raises(ValueError, match="unambiguous identity"):
            load_studio_config(path)


def test_codebuild_approval_values_are_strict_and_required_for_shared(tmp_path, monkeypatch):
    path = tmp_path / "shared.json"
    monkeypatch.setenv("STUDIO_PROXY_HMAC_SECRET", "test-only")
    monkeypatch.setenv("SHARED_RUNTIME_TOKEN", "test-only")
    base = {
        "schema_version": 2,
        "server": {"profile": "shared", "bind": "0.0.0.0", "public_base_url": "https://studio.example", "cors_origins": ["https://studio.example"]},
        "identity": {"proxy_hmac_secret_env": "STUDIO_PROXY_HMAC_SECRET", "trusted_peer_cidrs": ["127.0.0.1/32"]},
        "providers": {"artifact": "s3", "build": "codebuild", "runtime": "isolated-gateway"},
        "storage": {"s3_bucket": "private"},
        "build": {"codebuild_project": "toolbox", "network_preflight": "aws-vpc-endpoints-v1"},
        "runtime": {"gateway_url": "https://runtime.example", "gateway_token_env": "SHARED_RUNTIME_TOKEN", "gateway_allowed_hosts": ["runtime.example"]},
        "preview": {"bind": "0.0.0.0", "origin": "https://preview.example", "allowed_origins": ["https://preview.example"]},
    }
    path.write_text(json.dumps(base), encoding="utf-8")
    with pytest.raises(ValueError, match="approved_image_digest"):
        load_studio_config(path)
    base["build"].update({
        "approved_image_digest": "A" * 64,
        "approved_wrapper_sha256": "b" * 64,
        "probe_version": "probe-v1",
        "account_id": "111122223333",
        "region": "eu-west-1",
    })
    path.write_text(json.dumps(base), encoding="utf-8")
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        load_studio_config(path)


def test_configured_build_policies_are_preserved_and_invalid_values_fail(tmp_path):
    path = tmp_path / "policies.json"
    path.write_text(json.dumps({
        "schema_version": 2,
        "git": {"require_clean": True},
        "file_policy": {"exclusions": ["generated", "scratch.tmp"]},
        "compat": {"submit_wait_seconds": 12},
        "limits": {
            "request_body_bytes": 1024,
            "editor_body_bytes": 2048,
            "manifest_upload_body_bytes": 4096,
            "snapshot_files": 10,
            "snapshot_bytes": 1000,
            "artifact_bytes": 1000,
            "log_phase_bytes": 512,
            "build_timeout_seconds": 30,
            "builds_global": 2,
            "builds_per_project": 1,
            "storage_spool_bytes": 3000,
            "storage_min_free_bytes": 1,
        },
    }), encoding="utf-8")
    config = load_studio_config(path)
    assert config.git_require_clean is True
    assert config.file_exclusions == ("generated", "scratch.tmp")
    assert config.submit_wait_seconds == 12
    assert config.request_body_limit_bytes == 1024
    assert config.build_timeout_seconds == 30
    assert config.storage_spool_max_bytes == 3000

    path.write_text(json.dumps({"schema_version": 2, "file_policy": {"exclusions": ["../escape"]}}), encoding="utf-8")
    with pytest.raises(ValueError, match="file_policy.exclusions"):
        load_studio_config(path)
    path.write_text(json.dumps({"schema_version": 2, "compat": {"submit_wait_seconds": 0}}), encoding="utf-8")
    with pytest.raises(ValueError, match="submit_wait_seconds"):
        load_studio_config(path)


def test_public_and_review_urls_reject_credentials_query_fragment_and_ambiguous_hosts(tmp_path):
    path = tmp_path / "urls.json"
    invalid_urls = (
        "http://user@127.0.0.1:5173",
        "http://127.0.0.1:5173/?token=secret",
        "http://127.0.0.1:5173/#secret",
        "http://example.test",
        "http://１２７.0.0.1:5173",
    )
    for url in invalid_urls:
        path.write_text(json.dumps({"schema_version": 2, "server": {"public_base_url": url}}), encoding="utf-8")
        with pytest.raises(ValueError, match="config_invalid"):
            load_studio_config(path)

    path.write_text(json.dumps({
        "schema_version": 2,
        "server": {
            "profile": "shared",
            "bind": "0.0.0.0",
            "public_base_url": "https://studio.example",
            "cors_origins": ["https://other.example"],
        },
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="allowlisted"):
        load_studio_config(path)


def test_invalid_json_and_unsafe_shared_fail_closed(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text("{invalid", encoding="utf-8")
    with pytest.raises(ValueError, match="config_invalid"):
        load_studio_config(path)
    path.write_text(json.dumps({"schema_version": 2, "server": {"profile": "shared", "bind": "0.0.0.0", "public_base_url": "https://studio.example", "cors_origins": ["https://studio.example"]}, "preview": {"bind": "127.0.0.1", "origin": "https://preview.example", "allowed_origins": ["https://preview.example"]}}), encoding="utf-8")
    with pytest.raises(ValueError, match="isolated providers"):
        load_studio_config(path)
