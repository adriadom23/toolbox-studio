from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from app.submissions.logs import LocalLogStore
from app.submissions.providers import NotificationEnvelope, ProviderResult, PublicationResult
from app.submissions.stores import FilesystemArtifactStore


class LocalNotificationProvider:
    def __init__(self, audit_path: Path) -> None:
        self.audit_path = Path(audit_path)

    def send(self, effect_id: str, generation: int, envelope: NotificationEnvelope) -> ProviderResult:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        existing = self.audit_path.read_text(encoding="utf-8").splitlines() if self.audit_path.is_file() else []
        marker = f"{effect_id}:{generation}"
        if not any(line.startswith(marker + "\t") for line in existing):
            safe = {"project": envelope.project, "version": envelope.version, "short_id": envelope.short_id, "author": envelope.author, "state": envelope.state, "review_url": envelope.review_url}
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(marker + "\t" + json.dumps(safe, sort_keys=True) + "\n")
        return ProviderResult(True, marker)


class LocalPublicationProvider:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def publish(self, effect_id: str, generation: int, submission: dict[str, object], target: str) -> PublicationResult:
        if target != "local-index":
            return PublicationResult(False)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{effect_id}.json"
        payload = {"submission_id": submission["id"], "artifact_ref": submission["artifact_ref"], "target": target}
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        if path.is_file():
            matches = path.read_text(encoding="utf-8") == encoded
            if matches:
                path.chmod(0o444)
                verified = path.stat()
                matches = path.read_text(encoding="utf-8") == encoded and not verified.st_mode & 0o222
            return PublicationResult(matches, path.name if matches else None)
        staging = self.root / f".{effect_id}.staging-{generation}-{secrets.token_hex(8)}"
        retained = self.root / f".{effect_id}.retained-readonly-{generation}-{secrets.token_hex(8)}"
        with staging.open("xb") as handle:
            handle.write(encoded.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        if staging.read_text(encoding="utf-8") != encoded:
            return PublicationResult(False)
        try:
            os.link(staging, path)
        except FileExistsError:
            pass
        if not path.is_file() or path.read_text(encoding="utf-8") != encoded:
            return PublicationResult(False)
        staging.replace(retained)
        retained.chmod(0o444)
        path.chmod(0o444)
        retained_stat = retained.stat()
        published_stat = path.stat()
        if (
            retained.read_text(encoding="utf-8") != encoded
            or path.read_text(encoding="utf-8") != encoded
            or not os.path.samefile(retained, path)
            or retained_stat.st_size != len(encoded.encode("utf-8"))
            or published_stat.st_size != retained_stat.st_size
            or retained_stat.st_mode & 0o222
            or published_stat.st_mode & 0o222
        ):
            return PublicationResult(False)
        return PublicationResult(True, path.name)


__all__ = ["FilesystemArtifactStore", "LocalLogStore", "LocalNotificationProvider", "LocalPublicationProvider"]
