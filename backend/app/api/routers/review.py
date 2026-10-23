from __future__ import annotations

import re

from fastapi import APIRouter, Request

from app.api.schemas import CommentCreate, EffectRetry, PublicationCreate, ReviewAction
from app.core.errors import PublicError
from app.submissions.database import connect, transaction
from app.submissions.outbox import OutboxDispatcher
from app.submissions.publication import PublicationService
from app.submissions.review import ReviewService, can_view, require_visible

_ID_RE = re.compile(r"^sub_[0-9a-f]{32}$")
_EFFECT_RE = re.compile(r"^eff_[0-9a-f]{32}$")


def actor_roles(request: Request) -> tuple[str, set[str]]:
    actor = request.state.identity
    roles = set(getattr(request.state, "roles", ()))
    return actor, roles


def actor_display(request: Request) -> str:
    return str(getattr(request.state, "display_name", request.state.identity))


def _validate_submission_id(submission_id: str) -> None:
    if not _ID_RE.fullmatch(submission_id):
        raise PublicError("invalid_submission_id", "invalid submission id", 422)


def create_review_router(database_path, providers, review_base_url: str) -> APIRouter:
    router = APIRouter(prefix="/api")
    reviews = ReviewService(database_path, providers=providers, review_base_url=review_base_url)
    publications = PublicationService(database_path, providers)

    @router.get("/submissions/{submission_id}/comments")
    def comments(submission_id: str, request: Request):
        _validate_submission_id(submission_id)
        actor, roles = actor_roles(request)
        connection = connect(database_path)
        try:
            row = connection.execute("SELECT * FROM submission_records WHERE id=?", (submission_id,)).fetchone()
            require_visible(row, actor, roles)
            rows = connection.execute("SELECT id,submission_id,seq,author_id AS author,body,created_at FROM submission_comments WHERE submission_id=? ORDER BY seq", (submission_id,)).fetchall()
            return {"items": [dict(item) for item in rows]}
        finally:
            connection.close()

    @router.post("/submissions/{submission_id}/comments", status_code=201)
    def add_comment(submission_id: str, body: CommentCreate, request: Request):
        _validate_submission_id(submission_id)
        actor, roles = actor_roles(request)
        return reviews.comment(
            submission_id, actor=actor, roles=roles, text=body.text,
            correlation_id=str(getattr(request.state, "correlation_id", "")),
        )

    @router.post("/submissions/{submission_id}/review")
    def review(submission_id: str, body: ReviewAction, request: Request):
        _validate_submission_id(submission_id)
        actor, roles = actor_roles(request)
        result = reviews.transition(
            submission_id, action=body.action, expected_row_version=body.expected_row_version,
            actor=actor, roles=roles, reason=body.reason or "", publication_target=body.publication_target,
            correlation_id=str(getattr(request.state, "correlation_id", "")),
        )
        return result.__dict__

    @router.post("/submissions/{submission_id}/publish")
    def publish(submission_id: str, body: PublicationCreate, request: Request):
        _validate_submission_id(submission_id)
        actor, roles = actor_roles(request)
        return publications.publish(
            submission_id,
            target=body.target,
            expected_row_version=body.expected_row_version,
            actor=actor,
            roles=roles,
            correlation_id=str(getattr(request.state, "correlation_id", "")),
        )

    @router.post("/submissions/{submission_id}/effects/{effect_id}/retry")
    def retry(submission_id: str, effect_id: str, body: EffectRetry, request: Request):
        _validate_submission_id(submission_id)
        if not _EFFECT_RE.fullmatch(effect_id):
            raise PublicError("invalid_effect_id", "invalid effect id", 422)
        actor, roles = actor_roles(request)
        if "admin" not in roles:
            connection = connect(database_path)
            try:
                require_visible(connection.execute("SELECT * FROM submission_records WHERE id=?", (submission_id,)).fetchone(), actor, roles)
            finally:
                connection.close()
            raise PublicError("forbidden", "admin capability required", 403)
        connection = connect(database_path)
        try:
            effect = connection.execute("SELECT * FROM outbox_effects WHERE id=? AND submission_id=?", (effect_id, submission_id)).fetchone()
            if effect is None:
                raise PublicError("not_found", "effect not found", 404)
            kind = effect["kind"]
        finally:
            connection.close()
        correlation_id = str(getattr(request.state, "correlation_id", ""))
        if kind == "publication":
            return publications.retry(
                submission_id,
                effect_id,
                expected_generation=body.expected_generation,
                actor=actor,
                roles=roles,
                correlation_id=correlation_id,
            )
        connection = connect(database_path)
        try:
            return OutboxDispatcher(connection, providers).retry(
                effect_id,
                body.expected_generation,
                actor=actor,
                roles=roles,
                correlation_id=correlation_id,
            )
        finally:
            connection.close()

    return router
