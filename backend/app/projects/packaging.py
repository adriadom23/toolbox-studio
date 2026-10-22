"""Submit-code packaging + owner-notification (design.md §4.4/§7).

``build_package(name)`` zips the project's two folders plus a
``MANIFEST.json`` (§7.1). ``notify_owners(name, package_path,
submitted_by)`` is a direct, literal reuse of the two vendored functions
named in FR-4 — ``send_email``/``post_message`` — exactly matching their
confirmed real signatures (§4.4). Neither function invents a new
transport; both already return False/a non-``ok`` dict rather than
raising, so a notification failure here is never treated as a packaging
failure by the caller (§4.6).
"""
from __future__ import annotations

import json
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path

from app.projects.registry import BACKEND_PROJECTS_DIR, FRONTEND_PROJECTS_DIR, BACKEND_DIR


class ProjectNotFoundError(ValueError):
    """Raised when ``build_package`` is called for a project whose folders
    are missing on disk."""


SUBMISSIONS_DIR = BACKEND_DIR / "submissions"


def build_package(project_name: str) -> Path:
    fe_dir = FRONTEND_PROJECTS_DIR / project_name
    be_dir = BACKEND_PROJECTS_DIR / project_name
    if not fe_dir.exists() or not be_dir.exists():
        raise ProjectNotFoundError(project_name)

    submissions_dir = SUBMISSIONS_DIR / project_name
    submissions_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    zip_path = submissions_dir / f"{project_name}-{stamp}.zip"

    # The backend half accumulates `__pycache__/*.pyc` the moment
    # `register_all_project_blueprints` (app/projects/registry.py) imports
    # its routes.py — host-specific compiled bytecode, not source the
    # owner reviewing the submission needs. Exclude it the same way a
    # `.gitignore` would (design.md's own `backend/.gitignore` already
    # excludes `__pycache__/` from the Studio repo itself; the submitted
    # package should hold the same standard).
    _SKIP_DIR_NAMES = {"__pycache__"}

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for base, arc_prefix in (
            (fe_dir, f"frontend/{project_name}"),
            (be_dir, f"backend/{project_name}"),
        ):
            for file_path in base.rglob("*"):
                if file_path.is_dir():
                    continue
                if _SKIP_DIR_NAMES & set(file_path.relative_to(base).parts[:-1]):
                    continue
                arcname = f"{arc_prefix}/{file_path.relative_to(base)}"
                zf.write(file_path, arcname)
        # A manifest the owner can read without extracting anything.
        zf.writestr(
            "MANIFEST.json",
            json.dumps(
                {
                    "project": project_name,
                    "packaged_at": datetime.now().isoformat(),
                    "frontend_root": f"frontend/src/projects/{project_name}/",
                    "backend_root": f"backend/app/projects/{project_name}/",
                },
                indent=2,
            ),
        )
    return zip_path


def _git_config_value(key: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "config", key],
            cwd=BACKEND_DIR.parent,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = (result.stdout or "").strip()
    return value or None


def _git_current_branch() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=BACKEND_DIR.parent,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = (result.stdout or "").strip()
    return value or None


def default_submitted_by() -> str | None:
    """Best-effort ``git config user.name``/``user.email`` lookup on the
    ToolboxStudio checkout. Returns ``None`` (never blocks the submission)
    when git isn't configured or isn't available."""

    name = _git_config_value("user.name")
    email = _git_config_value("user.email")
    if name and email:
        return f"{name} <{email}>"
    return name or email


def _build_notification_html(
    project_name: str, package_path: Path, submitted_by: str | None
) -> str:
    branch = _git_current_branch()
    return (
        "<h2>Toolbox Studio submission</h2>"
        f"<p><strong>Project:</strong> {project_name}</p>"
        f"<p><strong>Submitted by:</strong> {submitted_by or 'unknown'}</p>"
        f"<p><strong>Branch:</strong> {branch or 'unknown'}</p>"
        f"<p><strong>Package:</strong> <code>{package_path}</code></p>"
        "<p>Open the Toolbox Studio checkout on this machine (or ask the submitter "
        "for the zip) to review and integrate. Toolbox Studio does not push or "
        "open a PR on your behalf — integration is a manual copy, per the "
        "project's design.</p>"
    )


def notify_owners(
    project_name: str, package_path: Path, submitted_by: str | None = None
) -> dict[str, bool]:
    from app.core.owner_config import notification_targets
    from vendor.integrations.email_client import send_email
    from vendor.integrations.slack_client import post_message

    targets = notification_targets()
    results = {"email": False, "slack": False}
    subject = f"Toolbox Studio: project '{project_name}' ready to integrate"
    body_html = _build_notification_html(project_name, package_path, submitted_by)

    if targets["emails"]:
        try:
            results["email"] = bool(
                send_email(
                    to="; ".join(targets["emails"]),
                    subject=subject,
                    body_html=body_html,
                    attachment=None,  # the zip stays on disk; email links to it
                )
            )
        except Exception:  # noqa: BLE001 — notification is never fatal (§4.6)
            results["email"] = False

    if targets["slack_webhook"]:
        text = (
            f"\U0001F4E6 *Toolbox Studio submission*: *{project_name}*\n"
            f"Submitted by: {submitted_by or 'unknown'}\n"
            f"Package: `{package_path}`\n"
            f"Run `cd ToolboxStudio && git log -1 -- frontend/src/projects/{project_name} "
            f"backend/app/projects/{project_name}` to see the latest commit touching it."
        )
        try:
            slack_result = post_message(targets["slack_webhook"], text)
            results["slack"] = bool(slack_result.get("ok"))
        except Exception:  # noqa: BLE001 — notification is never fatal (§4.6)
            results["slack"] = False

    return results
