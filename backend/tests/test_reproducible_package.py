from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from app.submissions.packaging import create_reproducible_package, verify_package
from app.submissions.snapshot import SafeSnapshotBuilder


def test_two_identical_snapshots_create_identical_zip(tmp_path):
    frontend = tmp_path / "frontend" / "src" / "projects"
    backend = tmp_path / "backend" / "app" / "projects"
    (frontend / "demo").mkdir(parents=True)
    (backend / "demo").mkdir(parents=True)
    (frontend / "demo" / "Demo.vue").write_text("<template><p>ok</p></template>")
    (backend / "demo" / "routes.py").write_text("source")
    builder = SafeSnapshotBuilder(frontend, backend)
    one = builder.build("demo", tmp_path / "snapshot-one")
    two = builder.build("demo", tmp_path / "snapshot-two")
    first = create_reproducible_package(one, tmp_path / "one.zip", submission_id="sub_fixed", version=1, harness_sha256="h")
    second = create_reproducible_package(two, tmp_path / "two.zip", submission_id="sub_fixed", version=1, harness_sha256="h")
    assert first.sha256 == second.sha256
    with zipfile.ZipFile(first.path) as archive:
        assert all("\\" not in name and not name.startswith("/") for name in archive.namelist())


def test_package_rejects_snapshot_entry_replaced_after_build(tmp_path):
    frontend = tmp_path / "frontend" / "src" / "projects"
    backend = tmp_path / "backend" / "app" / "projects"
    (frontend / "demo").mkdir(parents=True)
    (backend / "demo").mkdir(parents=True)
    (frontend / "demo" / "Demo.vue").write_text("original", encoding="utf-8")
    (backend / "demo" / "routes.py").write_text("routes", encoding="utf-8")
    snapshot = SafeSnapshotBuilder(frontend, backend).build("demo", tmp_path / "snapshot")
    sealed = snapshot.root / "frontend" / "src" / "projects" / "demo" / "Demo.vue"
    retained = tmp_path / "original-Demo.retained"
    sealed.rename(retained)
    sealed.write_text("replacement with different bytes", encoding="utf-8")

    with pytest.raises(ValueError, match="changed before packaging|integrity mismatch"):
        create_reproducible_package(
            snapshot,
            tmp_path / "replaced.zip",
            submission_id="sub_fixed",
            version=1,
            harness_sha256="h",
        )
    assert retained.read_text(encoding="utf-8") == "original"


def _rewrite_package(source: Path, destination: Path, manifest: dict[str, object], additions: dict[str, bytes]) -> None:
    with zipfile.ZipFile(source) as original:
        payloads = {name: original.read(name) for name in original.namelist() if name != "MANIFEST.json"}
    payloads.update(additions)
    with zipfile.ZipFile(destination, "w") as archive:
        for name in sorted(payloads):
            archive.writestr(name, payloads[name])
        archive.writestr("MANIFEST.json", json.dumps(manifest, sort_keys=True, separators=(",", ":")))


def test_package_verification_rejects_unmanifested_and_noncanonical_entries(tmp_path):
    frontend = tmp_path / "frontend" / "src" / "projects"
    backend = tmp_path / "backend" / "app" / "projects"
    (frontend / "demo").mkdir(parents=True)
    (backend / "demo").mkdir(parents=True)
    (frontend / "demo" / "Demo.vue").write_text("demo", encoding="utf-8")
    (backend / "demo" / "routes.py").write_text("routes", encoding="utf-8")
    snapshot = SafeSnapshotBuilder(frontend, backend).build("demo", tmp_path / "snapshot")
    package = create_reproducible_package(snapshot, tmp_path / "valid.zip", submission_id="sub_fixed", version=1, harness_sha256="h")

    extra = tmp_path / "extra.zip"
    _rewrite_package(package.path, extra, package.manifest, {"frontend/demo/extra.txt": b"untracked"})
    with pytest.raises(ValueError, match="exactly match"):
        verify_package(extra)

    semantic = tmp_path / "semantic.zip"
    semantic_manifest = json.loads(json.dumps(package.manifest))
    duplicate_data = b"demo"
    semantic_manifest["files"].append({
        "path": "frontend/demo/demo.vue", "size": len(duplicate_data),
        "sha256": hashlib.sha256(duplicate_data).hexdigest(), "kind": "frontend",
    })
    semantic_manifest["files"] = sorted(semantic_manifest["files"], key=lambda item: item["path"])
    _rewrite_package(package.path, semantic, semantic_manifest, {"frontend/demo/demo.vue": duplicate_data})
    with pytest.raises(ValueError, match="not unique"):
        verify_package(semantic)

    traversal = tmp_path / "traversal.zip"
    traversal_manifest = json.loads(json.dumps(package.manifest))
    traversal_data = b"escape"
    traversal_manifest["files"].append({
        "path": "frontend/demo/../escape.txt", "size": len(traversal_data),
        "sha256": hashlib.sha256(traversal_data).hexdigest(), "kind": "frontend",
    })
    traversal_manifest["files"] = sorted(traversal_manifest["files"], key=lambda item: item["path"])
    _rewrite_package(package.path, traversal, traversal_manifest, {"frontend/demo/../escape.txt": traversal_data})
    with pytest.raises(ValueError, match="path is invalid"):
        verify_package(traversal)
