from __future__ import annotations

import hashlib
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.submissions.local_providers import LocalNotificationProvider, LocalPublicationProvider
from app.submissions.providers import NotificationEnvelope
from app.submissions.stores import FilesystemArtifactStore, LegacyArtifactStore


def test_filesystem_store_is_immutable_and_notification_is_idempotent(tmp_path):
    source = tmp_path / "source.zip"
    source.write_bytes(b"artifact")
    digest = hashlib.sha256(b"artifact").hexdigest()
    store = FilesystemArtifactStore(tmp_path / "objects")
    first = store.put_if_absent("art:v1:one", source, 8, digest)
    second = store.put_if_absent("art:v1:one", source, 8, digest)
    assert first == second and store.stat(first).sha256 == digest
    notifier = LocalNotificationProvider(tmp_path / "audit.jsonl")
    envelope = NotificationEnvelope("demo", "v1", "abcd", "alice", "submitted", "/submissions/1")
    notifier.send("effect", 1, envelope)
    notifier.send("effect", 1, envelope)
    assert len((tmp_path / "audit.jsonl").read_text().splitlines()) == 1
    assert "boto3" not in sys.modules


def test_filesystem_store_never_exposes_partial_final_object(tmp_path, monkeypatch):
    source = tmp_path / "source.zip"
    source.write_bytes(b"complete")
    digest = hashlib.sha256(b"complete").hexdigest()
    store = FilesystemArtifactStore(tmp_path / "objects")

    def crash_before_promotion(_source, _target):
        raise OSError("simulated crash")

    monkeypatch.setattr("app.submissions.stores.os.link", crash_before_promotion)
    try:
        store.put_if_absent("art:v1:crash", source, 8, digest)
    except OSError:
        pass
    assert not store._path("art:v1:crash").exists()
    assert list((tmp_path / "objects").rglob("*.staging-*"))


def test_filesystem_store_concurrent_put_is_exclusive_and_quarantines_loser(tmp_path, monkeypatch):
    first_source = tmp_path / "first.zip"
    second_source = tmp_path / "second.zip"
    first_source.write_bytes(b"first artifact")
    second_source.write_bytes(b"second artifact")
    store = FilesystemArtifactStore(tmp_path / "objects")
    original_link = __import__("os").link
    barrier = threading.Barrier(2)

    def racing_link(source, target):
        barrier.wait(timeout=5)
        return original_link(source, target)

    monkeypatch.setattr("app.submissions.stores.os.link", racing_link)

    def put(source):
        payload = source.read_bytes()
        return store.put_if_absent(
            "art:v1:race",
            source,
            len(payload),
            hashlib.sha256(payload).hexdigest(),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [future.exception() for future in (executor.submit(put, first_source), executor.submit(put, second_source))]

    assert sum(outcome is None for outcome in outcomes) == 1
    assert sum(isinstance(outcome, FileExistsError) for outcome in outcomes) == 1
    target = store._path("art:v1:race")
    assert target.read_bytes() in {b"first artifact", b"second artifact"}
    assert len(list(target.parent.glob(f".{target.name}.quarantine-*"))) == 1
    assert len(list(target.parent.glob(f".{target.name}.promoted-*"))) == 1
    assert list(target.parent.glob(f".{target.name}.staging-*")) == []


def test_verified_local_stream_keeps_the_validated_descriptor_after_path_replacement(tmp_path):
    source = tmp_path / "source.zip"
    source.write_bytes(b"authorized artifact")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    store = FilesystemArtifactStore(tmp_path / "objects")
    ref = store.put_if_absent("art:v1:stable", source, source.stat().st_size, digest)
    target = store._path(ref.key)
    moved = target.with_name(target.name + ".moved")
    opened = store.open_verified(ref, size=len(b"authorized artifact"), sha256=digest)
    handle, verified = opened.__enter__()
    try:
        with pytest.raises(OSError):
            target.rename(moved)
        assert handle.read() == b"authorized artifact"
        assert (verified.size, verified.sha256) == (len(b"authorized artifact"), digest)
    finally:
        opened.__exit__(None, None, None)


def test_verified_local_stream_blocks_in_place_mutation_until_stream_closes(tmp_path):
    source = tmp_path / "source.zip"
    source.write_bytes(b"sealed artifact")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    store = FilesystemArtifactStore(tmp_path / "objects")
    ref = store.put_if_absent("art:v1:sealed", source, source.stat().st_size, digest)
    target = store._path(ref.key)
    opened = store.open_verified(ref, size=source.stat().st_size, sha256=digest)
    handle, verified = opened.__enter__()
    try:
        with pytest.raises(OSError):
            with target.open("r+b") as writer:
                writer.truncate(1)
        assert handle.read() == b"sealed artifact"
        assert (verified.size, verified.sha256) == (len(b"sealed artifact"), digest)
    finally:
        opened.__exit__(None, None, None)
        target.chmod(0o600)


def test_verified_legacy_stream_keeps_the_validated_descriptor_after_path_replacement(tmp_path):
    root = tmp_path / "legacy"
    path = root / "demo" / "legacy.zip"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"legacy artifact")
    info = path.lstat()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    store = LegacyArtifactStore(root)
    opened = store.open_source(
        "demo/legacy.zip", file_id=f"{info.st_dev}:{info.st_ino}", size=info.st_size,
        mtime_ns=info.st_mtime_ns, sha256=digest,
    )
    handle = opened.__enter__()
    try:
        moved = path.with_name("legacy-original.zip")
        with pytest.raises(OSError):
            path.rename(moved)
        assert handle.read() == b"legacy artifact"
    finally:
        opened.__exit__(None, None, None)


def test_local_publication_promotes_only_complete_verified_json(tmp_path, monkeypatch):
    provider = LocalPublicationProvider(tmp_path / "published")
    submission = {"id": "sub_1", "artifact_ref": "immutable"}

    def fail_link(_source, _destination):
        raise OSError("simulated promotion interruption")

    monkeypatch.setattr("app.submissions.local_providers.os.link", fail_link)
    try:
        provider.publish("eff_1", 1, submission, "local-index")
    except OSError:
        pass
    assert not (tmp_path / "published" / "eff_1.json").exists()
    assert list((tmp_path / "published").glob(".eff_1.staging-*"))


def test_local_publication_seals_retained_alias_against_mutation(tmp_path):
    provider = LocalPublicationProvider(tmp_path / "published")
    submission = {"id": "sub_1", "artifact_ref": "immutable"}
    result = provider.publish("eff_1", 1, submission, "local-index")
    assert result.ok
    published = tmp_path / "published" / "eff_1.json"
    retained = next((tmp_path / "published").glob(".eff_1.retained-readonly-*"))
    original = published.read_bytes()
    assert not list((tmp_path / "published").glob(".eff_1.staging-*"))
    assert __import__("os").path.samefile(published, retained)
    with pytest.raises(OSError):
        retained.write_bytes(b"mutated")
    assert published.read_bytes() == original
