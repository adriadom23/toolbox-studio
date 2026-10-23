from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from app.submissions.providers import GitMetadata

_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")


class LocalGitMetadataProvider:
    def _run(self, root: Path, args: list[str], timeout: int) -> tuple[int, str]:
        try:
            result = subprocess.run(["git", *args], cwd=root, shell=False, capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.SubprocessError):
            return 1, ""
        return result.returncode, (result.stdout or "").strip()

    def inspect(self, checkout_root: Path, timeout_seconds: int = 5) -> GitMetadata:
        warnings: list[str] = []
        commit_code, commit = self._run(checkout_root, ["rev-parse", "HEAD"], timeout_seconds)
        branch_code, branch = self._run(checkout_root, ["rev-parse", "--abbrev-ref", "HEAD"], timeout_seconds)
        dirty_code, dirty_output = self._run(checkout_root, ["status", "--porcelain"], timeout_seconds)
        remote_code, remote = self._run(checkout_root, ["config", "--get", "remote.origin.url"], timeout_seconds)
        if commit_code or not _COMMIT_RE.fullmatch(commit):
            commit = None
            warnings.append("git_commit_unavailable")
        if branch_code or not _BRANCH_RE.fullmatch(branch) or branch.startswith("-"):
            branch = None
            warnings.append("git_branch_unavailable")
        sanitized_remote = None
        if not remote_code and remote:
            try:
                parsed = urlsplit(remote)
                if parsed.scheme in {"http", "https", "ssh"} and parsed.hostname:
                    host = parsed.hostname + (f":{parsed.port}" if parsed.port else "")
                    sanitized_remote = urlunsplit((parsed.scheme, host, parsed.path, "", ""))
                elif re.fullmatch(r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[A-Za-z0-9._/-]+", remote):
                    sanitized_remote = remote.split("@", 1)[1]
            except ValueError:
                sanitized_remote = None
        if remote and sanitized_remote is None:
            warnings.append("git_remote_omitted")
        return GitMetadata(commit, branch, sanitized_remote, bool(dirty_output) if not dirty_code else None, tuple(warnings))
