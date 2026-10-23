from __future__ import annotations

import json
import math
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from app.core.errors import PublicError
from app.core.studio_config import validate_safe_url
from app.submissions.database import transaction
from app.submissions.idempotency import complete
from app.submissions.providers import NotificationEnvelope
from app.submissions.repository_v2 import PipelineRepository, now_iso


def _before_retry_audit() -> None:
    """Fault-injection boundary between retry state and its audit event."""


def _after_effect_delivery_commit() -> None:
    """Fault-injection boundary before publication domain reconciliation."""


_NOTIFICATION_FIELDS = ("project", "version", "short_id", "author", "state", "review_url")


def minimal_notification_payload(envelope: NotificationEnvelope) -> dict[str, str]:
    return {name: str(getattr(envelope, name)) for name in _NOTIFICATION_FIELDS}


def notification_channels(providers) -> tuple[str, ...]:
    configured = getattr(providers, "notifications", None)
    if isinstance(configured, dict):
        return tuple(configured)
    identities = getattr(providers, "identities", {})
    channels = identities.get("notifications", ()) if isinstance(identities, dict) else ()
    if channels:
        return tuple(str(item) for item in channels)
    return ("local",)


def enqueue_notification(
    connection,
    submission,
    *,
    state: str,
    review_base_url: str,
    channels: tuple[str, ...] = ("local",),
) -> list[str]:
    safe_base = validate_safe_url(review_base_url, "review_url")
    allowed_host = urlparse(safe_base).hostname or ""
    review_url = validate_safe_url(
        f"{safe_base}/submissions/{submission['id']}",
        "review_url",
        https_required=urlparse(safe_base).scheme == "https",
        allowed_hosts={allowed_host},
    )
    envelope = NotificationEnvelope(
        project=submission["project_slug"], version=submission["version_label"],
        short_id=submission["id"][-8:], author=submission["author_display"],
        state=state, review_url=review_url,
    )
    payload = minimal_notification_payload(envelope)
    return [
        enqueue_effect(connection, submission["id"], "notification", payload, provider_key=channel)
        for channel in channels
    ]


def enqueue_effect(
    connection,
    submission_id: str,
    kind: str,
    payload: dict[str, object],
    *,
    provider_key: str | None = None,
) -> str:
    if kind not in {"notification", "publication"}:
        raise ValueError("unsupported effect kind")
    if kind == "notification" and provider_key is not None and provider_key not in {"local", "ses", "teams"}:
        raise ValueError("unsupported notification provider")
    effect_id = "eff_" + secrets.token_hex(16)
    now = now_iso()
    connection.execute(
        "INSERT INTO outbox_effects(id,submission_id,kind,generation,status,payload_json,provider_key,created_at,updated_at) "
        "VALUES (?,?,?,1,'pending',?,?,?,?)",
        (
            effect_id,
            submission_id,
            kind,
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            provider_key,
            now,
            now,
        ),
    )
    return effect_id


def effect_retry_state(connection, row, *, now: datetime | None = None) -> tuple[bool, int]:
    if row["status"] != "delivery_unknown" or not row["lease_owner"] or not row["lease_until"]:
        return False, 0
    current = now or datetime.now(timezone.utc)
    try:
        lease_until = datetime.fromisoformat(str(row["lease_until"]))
    except ValueError:
        return False, 0
    if lease_until.tzinfo is None:
        lease_until = lease_until.replace(tzinfo=timezone.utc)
    if lease_until <= current:
        return False, 0
    attempt = connection.execute(
        "SELECT status FROM effect_attempts WHERE effect_id=? AND generation=? ORDER BY attempt DESC LIMIT 1",
        (row["id"], row["generation"]),
    ).fetchone()
    if attempt is None or attempt["status"] != "sending":
        return False, 0
    return True, max(1, math.ceil((lease_until - current).total_seconds()))


def reconcile_delivered_publication(connection, effect_id: str) -> bool:
    with transaction(connection):
        row = connection.execute(
            "SELECT * FROM outbox_effects WHERE id=? AND kind='publication' AND status='delivered'",
            (effect_id,),
        ).fetchone()
        if row is None:
            return False
        now = now_iso()
        transition = connection.execute(
            "UPDATE submission_records SET state='published',row_version=row_version+1,updated_at=? WHERE id=? AND state='approved'",
            (now, row["submission_id"]),
        )
        if not transition.rowcount:
            return False
        payload = json.loads(row["payload_json"])
        retry_event = connection.execute(
            "SELECT actor_id,actor_roles_json,correlation_id FROM effect_retry_events WHERE effect_id=? AND generation=?",
            (effect_id, row["generation"]),
        ).fetchone()
        event_actor = str(retry_event["actor_id"]) if retry_event else str(payload.get("actor_id") or "system")
        event_roles = json.loads(retry_event["actor_roles_json"]) if retry_event else payload.get("actor_roles", [])
        event_correlation = str(retry_event["correlation_id"] or "") if retry_event else str(payload.get("correlation_id") or "")
        PipelineRepository(connection).append_event(
            str(row["submission_id"]),
            "published",
            event_actor,
            {
                "effect_id": effect_id,
                "reference": row["provider_id"],
                "target": payload.get("target"),
                "origin_actor_id": payload.get("actor_id"),
            },
            actor_roles=tuple(str(item) for item in event_roles),
            old_state="approved",
            new_state="published",
            correlation_id=event_correlation,
        )
        return True


def reconcile_notification_phase(connection, submission_id: str) -> str:
    rows = connection.execute(
        "SELECT status FROM outbox_effects WHERE submission_id=? AND kind='notification' ORDER BY created_at",
        (submission_id,),
    ).fetchall()
    statuses = [str(row["status"]) for row in rows]
    if not statuses:
        phase_status, error_code = "succeeded", None
    elif any(status == "pending" for status in statuses):
        phase_status, error_code = "running", None
    elif any(status == "delivery_unknown" for status in statuses):
        phase_status, error_code = "delivery_unknown", "notification_delivery_unknown"
    elif all(status == "delivered" for status in statuses):
        phase_status, error_code = "succeeded", None
    else:
        phase_status, error_code = "failed", "notification_failed"
    connection.execute(
        "INSERT INTO submission_phases(submission_id,phase,status,error_code,updated_at) VALUES (?,'notify',?,?,?) "
        "ON CONFLICT(submission_id,phase) DO UPDATE SET status=excluded.status,error_code=excluded.error_code,updated_at=excluded.updated_at",
        (submission_id, phase_status, error_code, now_iso()),
    )
    if phase_status in {"succeeded", "failed", "delivery_unknown"}:
        from app.submissions.saga import build_operation_response, operation_success_status

        submission = PipelineRepository(connection).get(submission_id)
        operations = connection.execute(
            "SELECT operation_id FROM idempotency_records WHERE subject_id=?",
            (submission_id,),
        ).fetchall()
        for operation in operations:
            operation_id = str(operation["operation_id"])
            complete(
                connection,
                operation_id,
                operation_success_status(connection, operation_id),
                build_operation_response(connection, operation_id, submission),
            )
    return phase_status


class OutboxDispatcher:
    def __init__(self, connection, providers, worker_id: str | None = None) -> None:
        self.connection = connection
        self.providers = providers
        self.worker_id = worker_id or "outbox-" + secrets.token_hex(8)

    def _claim_and_mark_ambiguous(self, effect_id: str):
        now = datetime.now(timezone.utc)
        lease_until = (now + timedelta(seconds=60)).isoformat()
        with transaction(self.connection):
            cursor = self.connection.execute(
                "UPDATE outbox_effects SET status='delivery_unknown',last_error_code=kind||'_delivery_unknown',lease_owner=?,lease_generation=lease_generation+1,lease_until=?,updated_at=? WHERE id=? AND status='pending' AND (lease_until IS NULL OR lease_until<? OR lease_owner=?)",
                (self.worker_id, lease_until, now.isoformat(), effect_id, now.isoformat(), self.worker_id),
            )
            if cursor.rowcount != 1:
                return None
            row = self.connection.execute("SELECT * FROM outbox_effects WHERE id=?", (effect_id,)).fetchone()
            generation = int(row["generation"])
            attempt = int(self.connection.execute("SELECT COALESCE(MAX(attempt),0)+1 FROM effect_attempts WHERE effect_id=? AND generation=?", (effect_id, generation)).fetchone()[0])
            self.connection.execute(
                "INSERT INTO effect_attempts(effect_id,generation,attempt,status,error_code,created_at) VALUES (?,?,?,?,?,?)",
                (effect_id, generation, attempt, "sending", f"{row['kind']}_delivery_unknown", now.isoformat()),
            )
            # This transaction is deliberately before the external call. A
            # crash after delivery remains ambiguous and is never auto-sent.
            return row, attempt

    def _notification_provider(self, row):
        configured = self.providers.notifications
        if not isinstance(configured, dict):
            return configured
        provider_key = row["provider_key"]
        if provider_key is None and len(configured) == 1:
            return next(iter(configured.values()))
        provider = configured.get(str(provider_key))
        if provider is None:
            raise RuntimeError("configured notification provider is unavailable")
        return provider

    def dispatch(self, effect_id: str) -> dict[str, object]:
        existing = self.connection.execute("SELECT * FROM outbox_effects WHERE id=?", (effect_id,)).fetchone()
        if existing is None:
            raise PublicError("not_found", "effect not found", 404)
        if existing["status"] != "pending":
            if existing["kind"] == "notification":
                with transaction(self.connection):
                    reconcile_notification_phase(self.connection, str(existing["submission_id"]))
            return dict(self.connection.execute("SELECT * FROM outbox_effects WHERE id=?", (effect_id,)).fetchone())
        claimed = self._claim_and_mark_ambiguous(effect_id)
        if claimed is None:
            current = self.connection.execute("SELECT * FROM outbox_effects WHERE id=?", (effect_id,)).fetchone()
            return dict(current or existing)
        row, sending_attempt = claimed
        generation = int(row["generation"])
        lease_generation = int(row["lease_generation"])
        payload = json.loads(row["payload_json"])
        try:
            if row["kind"] == "notification":
                configured_base = validate_safe_url(getattr(self.providers, "review_base_url", payload["review_url"]), "review_url")
                allowed_host = urlparse(configured_base).hostname or ""
                payload["review_url"] = validate_safe_url(
                    payload["review_url"],
                    "review_url",
                    https_required=urlparse(configured_base).scheme == "https",
                    allowed_hosts={allowed_host},
                )
                envelope = NotificationEnvelope(**{key: payload[key] for key in _NOTIFICATION_FIELDS})
                result = self._notification_provider(row).send(effect_id, generation, envelope)
                reference = result.provider_id
            else:
                submission = self.connection.execute("SELECT * FROM submission_records WHERE id=?", (row["submission_id"],)).fetchone()
                result = self.providers.publication.publish(effect_id, generation, dict(submission), str(payload["target"]))
                reference = result.reference
            status = "delivered" if result.ok else "failed"
            error = None if result.ok else f"{row['kind']}_failed"
        except Exception:
            status, error, reference = "delivery_unknown", f"{row['kind']}_delivery_unknown", None
        now = now_iso()
        with transaction(self.connection):
            self.connection.execute(
                "INSERT INTO effect_attempts(effect_id,generation,attempt,status,error_code,created_at) VALUES (?,?,?,?,?,?)",
                (effect_id, generation, sending_attempt + 1, status, error, now),
            )
            cursor = self.connection.execute(
                "UPDATE outbox_effects SET status=?,provider_id=?,last_error_code=?,lease_owner=NULL,lease_until=NULL,updated_at=? WHERE id=? AND generation=? AND lease_generation=? AND lease_owner=? AND status='delivery_unknown'",
                (status, reference, error, now, effect_id, generation, lease_generation, self.worker_id),
            )
        if cursor.rowcount == 1 and row["kind"] == "publication" and status == "delivered":
            _after_effect_delivery_commit()
            reconcile_delivered_publication(self.connection, effect_id)
        if row["kind"] == "notification":
            with transaction(self.connection):
                reconcile_notification_phase(self.connection, str(row["submission_id"]))
        if cursor.rowcount != 1:
            return dict(self.connection.execute("SELECT * FROM outbox_effects WHERE id=?", (effect_id,)).fetchone())
        return dict(self.connection.execute("SELECT * FROM outbox_effects WHERE id=?", (effect_id,)).fetchone())

    def dispatch_pending(self, limit: int = 100) -> list[dict[str, object]]:
        batch_limit = min(max(limit, 1), 100)
        delivered = self.connection.execute(
            "SELECT e.id FROM outbox_effects e JOIN submission_records s ON s.id=e.submission_id "
            "WHERE e.kind='publication' AND e.status='delivered' AND s.state='approved' ORDER BY e.updated_at LIMIT ?",
            (batch_limit,),
        ).fetchall()
        for row in delivered:
            reconcile_delivered_publication(self.connection, row["id"])
        rows = self.connection.execute(
            "SELECT id FROM outbox_effects WHERE status='pending' ORDER BY created_at LIMIT ?",
            (batch_limit,),
        ).fetchall()
        return [self.dispatch(row["id"]) for row in rows]

    def retry(
        self,
        effect_id: str,
        expected_generation: int,
        *,
        actor: str,
        roles: set[str],
        correlation_id: str = "",
    ) -> dict[str, object]:
        now = datetime.now(timezone.utc)
        with transaction(self.connection):
            row = self.connection.execute("SELECT * FROM outbox_effects WHERE id=?", (effect_id,)).fetchone()
            if row is None:
                raise PublicError("not_found", "effect not found", 404)
            if int(row["generation"]) != expected_generation:
                raise PublicError("stale_effect", "effect generation changed", 409)
            if row["status"] == "delivered":
                return dict(row)
            in_flight, retry_after = effect_retry_state(self.connection, row, now=now)
            if in_flight:
                raise PublicError(
                    "effect_in_flight",
                    "effect delivery is already in progress",
                    409,
                    recoverable=True,
                    details={
                        "status": "in_progress",
                        "retry_after": retry_after,
                        "poll_url": f"/api/submissions/{row['submission_id']}",
                    },
                )
            updated_at = now.isoformat()
            cursor = self.connection.execute(
                "UPDATE outbox_effects SET generation=generation+1,status='pending',last_error_code=NULL,lease_owner=NULL,lease_until=NULL,updated_at=? WHERE id=? AND generation=?",
                (updated_at, effect_id, expected_generation),
            )
            if cursor.rowcount != 1:
                raise PublicError("stale_effect", "effect generation changed", 409)
            _before_retry_audit()
            self.connection.execute(
                "INSERT INTO effect_retry_events(effect_id,generation,actor_id,actor_roles_json,correlation_id,created_at) VALUES (?,?,?,?,?,?)",
                (effect_id, expected_generation + 1, actor, json.dumps(sorted(roles), separators=(",", ":")), correlation_id, updated_at),
            )
        return self.dispatch(effect_id)
