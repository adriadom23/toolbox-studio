from __future__ import annotations

import json

from app.core.errors import PublicError
from app.submissions.database import connect, transaction
from app.submissions.outbox import OutboxDispatcher, enqueue_effect
from app.submissions.repository_v2 import PipelineRepository, now_iso
from app.submissions.review import require_visible


class PublicationService:
    def __init__(self, database_path, providers) -> None:
        self.database_path = database_path
        self.providers = providers

    def publish(
        self,
        submission_id: str,
        *,
        target: str,
        expected_row_version: int,
        actor: str,
        roles: set[str],
        correlation_id: str = "",
    ) -> dict[str, object]:
        if "admin" not in roles:
            connection = connect(self.database_path)
            try:
                require_visible(connection.execute("SELECT * FROM submission_records WHERE id=?", (submission_id,)).fetchone(), actor, roles)
            finally:
                connection.close()
            raise PublicError("forbidden", "admin capability required", 403)
        if target != "local-index":
            raise PublicError("invalid_publication_target", "unsupported publication target", 422)
        connection = connect(self.database_path)
        try:
            with transaction(connection):
                row = connection.execute("SELECT * FROM submission_records WHERE id=?", (submission_id,)).fetchone()
                require_visible(row, actor, roles)
                if int(row["row_version"]) != expected_row_version:
                    raise PublicError("stale_submission", "submission changed", 409)
                if row["state"] != "approved":
                    raise PublicError("invalid_transition", "only approved submissions can be published", 409)
                existing = connection.execute("SELECT id FROM outbox_effects WHERE submission_id=? AND kind='publication' ORDER BY created_at DESC LIMIT 1", (submission_id,)).fetchone()
                effect_id = existing["id"] if existing else enqueue_effect(
                    connection,
                    submission_id,
                    "publication",
                    {
                        "target": target,
                        "actor_id": actor,
                        "actor_roles": sorted(roles),
                        "correlation_id": correlation_id,
                    },
                )
            return self._dispatch_and_commit(connection, effect_id)
        finally:
            connection.close()

    def retry(
        self,
        submission_id: str,
        effect_id: str,
        *,
        expected_generation: int,
        actor: str,
        roles: set[str],
        correlation_id: str = "",
    ) -> dict[str, object]:
        if "admin" not in roles:
            raise PublicError("forbidden", "admin capability required", 403)
        connection = connect(self.database_path)
        try:
            row = connection.execute("SELECT * FROM submission_records WHERE id=?", (submission_id,)).fetchone()
            require_visible(row, actor, roles)
            effect = connection.execute("SELECT * FROM outbox_effects WHERE id=? AND submission_id=?", (effect_id, submission_id)).fetchone()
            if effect is None:
                raise PublicError("not_found", "effect not found", 404)
            result = OutboxDispatcher(connection, self.providers).retry(
                effect_id,
                expected_generation,
                actor=actor,
                roles=roles,
                correlation_id=correlation_id,
            )
            with transaction(connection):
                return self._commit_if_delivered(connection, result)
        finally:
            connection.close()

    def _dispatch_and_commit(self, connection, effect_id: str) -> dict[str, object]:
        result = OutboxDispatcher(connection, self.providers).dispatch(effect_id)
        with transaction(connection):
            return self._commit_if_delivered(connection, result)

    def _commit_if_delivered(self, connection, effect: dict[str, object]) -> dict[str, object]:
        submission_id = str(effect["submission_id"])
        if effect["status"] == "delivered":
            payload = effect.get("payload_json")
            payload = json.loads(str(payload)) if payload else {}
            cursor = connection.execute("UPDATE submission_records SET state='published',row_version=row_version+1,updated_at=? WHERE id=? AND state='approved'", (now_iso(), submission_id))
            if cursor.rowcount:
                retry_event = connection.execute(
                    "SELECT actor_id,actor_roles_json,correlation_id FROM effect_retry_events WHERE effect_id=? AND generation=?",
                    (effect["id"], effect["generation"]),
                ).fetchone()
                event_actor = str(retry_event["actor_id"]) if retry_event else str(payload.get("actor_id") or "system")
                event_roles = json.loads(retry_event["actor_roles_json"]) if retry_event else payload.get("actor_roles", [])
                event_correlation = str(retry_event["correlation_id"] or "") if retry_event else str(payload.get("correlation_id") or "")
                PipelineRepository(connection).append_event(
                    submission_id,
                    "published",
                    event_actor,
                    {"effect_id": effect["id"], "reference": effect.get("provider_id"), "target": payload.get("target"), "origin_actor_id": payload.get("actor_id")},
                    actor_roles=tuple(str(item) for item in event_roles),
                    old_state="approved",
                    new_state="published",
                    correlation_id=event_correlation,
                )
        submission = connection.execute("SELECT state,row_version FROM submission_records WHERE id=?", (submission_id,)).fetchone()
        return {"effect_id": effect["id"], "generation": effect["generation"], "effect_status": effect["status"], "status": submission["state"], "row_version": submission["row_version"]}
