from __future__ import annotations

from app.core.errors import PublicError

_WORKSPACE_ROLES = {"submitter", "admin"}
_REVIEW_ROLES = {"reviewer", "admin"}


def current_membership(connection, project_slug: str, actor: str) -> bool:
    row = connection.execute(
        "SELECT active FROM project_memberships WHERE project_slug=? AND user_id=? ORDER BY version DESC LIMIT 1",
        (project_slug, actor),
    ).fetchone()
    return bool(row and row["active"])


def can_access_workspace(connection, project_slug: str, actor: str, roles: set[str]) -> bool:
    project = connection.execute("SELECT owner_id FROM projects WHERE slug=?", (project_slug,)).fetchone()
    if project is None:
        return False
    if "admin" in roles:
        return True
    return bool(roles & _WORKSPACE_ROLES) and (project["owner_id"] == actor or current_membership(connection, project_slug, actor))


def require_workspace(connection, project_slug: str, actor: str, roles: set[str], capability: str) -> None:
    project = connection.execute("SELECT owner_id FROM projects WHERE slug=?", (project_slug,)).fetchone()
    if project is None or ("admin" not in roles and project["owner_id"] != actor and not current_membership(connection, project_slug, actor)):
        raise PublicError("not_found", "project not found", 404)
    if not roles & _WORKSPACE_ROLES:
        raise PublicError("forbidden", f"{capability} capability required", 403)


def project_actions(connection, project_slug: str, actor: str, roles: set[str]) -> list[str]:
    if not can_access_workspace(connection, project_slug, actor, roles):
        return []
    actions = ["project.read"]
    if roles & _WORKSPACE_ROLES:
        actions.extend(["project.edit", "project.preview", "submission.create"])
    return actions


def can_view_submission(row, actor: str, roles: set[str]) -> bool:
    return bool(roles & _REVIEW_ROLES) or bool(row and row["author_id"] == actor)
