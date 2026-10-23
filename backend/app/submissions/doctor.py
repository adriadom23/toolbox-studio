from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from app.core.errors import PublicError
from app.submissions.database import connect, transaction
from app.submissions.providers import ObjectRef
from app.submissions.stores import FilesystemArtifactStore


def storage_plan(
    database_path: Path,
    runtime_root: Path,
    artifact_providers: dict[str, object] | None = None,
) -> dict[str, object]:
    connection = connect(database_path)
    providers = {"filesystem": FilesystemArtifactStore(Path(runtime_root) / "objects"), **(artifact_providers or {})}
    actions: list[dict[str, object]] = []
    diagnostics: list[dict[str, object]] = []
    try:
        rows = connection.execute("SELECT object_id,ref,expected_sha256,expected_size,state,provider FROM artifact_objects WHERE state IN ('reserved','committed','stored','uncertain') ORDER BY object_id").fetchall()
        for row in rows:
            if row["state"] == "reserved":
                continue
            provider_name = str(row["provider"])
            store = providers.get(provider_name)
            if store is None:
                diagnostics.append({"code": "provider_unsupported", "provider": provider_name, "object_id": row["object_id"]})
                continue
            try:
                current = store.stat(ObjectRef(provider_name, row["ref"]))
                valid = (current.size, current.sha256) == (row["expected_size"], row["expected_sha256"])
            except (OSError, ValueError):
                if provider_name != "filesystem":
                    diagnostics.append({"code": "provider_unavailable", "provider": provider_name, "object_id": row["object_id"]})
                    continue
                valid = False
            if not valid:
                actions.append({"kind": "quarantine_object", "object_id": row["object_id"], "from_state": row["state"]})
        now = datetime.now(timezone.utc).isoformat()
        expired = connection.execute("SELECT id,state FROM storage_reservations WHERE state='held' AND expires_at IS NOT NULL AND expires_at<? ORDER BY id", (now,)).fetchall()
        actions.extend({"kind": "release_reservation", "reservation_id": row["id"], "from_state": row["state"]} for row in expired)
    finally:
        connection.close()
    payload: dict[str, object] = {"version": 1, "actions": actions, "diagnostics": diagnostics, "physical_deletions": []}
    payload["plan_hash"] = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return payload


def apply_logical_plan(
    database_path: Path,
    runtime_root: Path,
    plan_hash: str,
    artifact_providers: dict[str, object] | None = None,
) -> dict[str, object]:
    plan = storage_plan(database_path, runtime_root, artifact_providers)
    if plan["plan_hash"] != plan_hash:
        raise PublicError("stale_doctor_plan", "storage state changed", 409)
    connection = connect(database_path)
    try:
        with transaction(connection):
            now = datetime.now(timezone.utc).isoformat()
            for action in plan["actions"]:
                if action["kind"] == "quarantine_object":
                    connection.execute("UPDATE artifact_objects SET state='quarantined',updated_at=? WHERE object_id=? AND state=?", (now, action["object_id"], action["from_state"]))
                elif action["kind"] == "release_reservation":
                    connection.execute("UPDATE storage_reservations SET state='released',updated_at=? WHERE id=? AND state='held'", (now, action["reservation_id"]))
    finally:
        connection.close()
    return {"plan_hash": plan_hash, "applied": len(plan["actions"]), "physical_deletions": []}
