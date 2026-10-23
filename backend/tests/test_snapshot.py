from __future__ import annotations

import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from app.core.errors import PublicError
from app.projects.safe_fs import StableRegularFile
from app.submissions.snapshot import SafeSnapshotBuilder


def roots(tmp_path):
    frontend = tmp_path / "frontend" / "src" / "projects"
    backend = tmp_path / "backend" / "app" / "projects"
    (frontend / "demo").mkdir(parents=True)
    (backend / "demo").mkdir(parents=True)
    (frontend / "demo" / "Demo.vue").write_text("<template />")
    (backend / "demo" / "routes.py").write_text("x=1")
    return frontend, backend


def test_snapshot_is_stable_and_omits_caches(tmp_path):
    frontend, backend = roots(tmp_path)
    (backend / "demo" / "__pycache__").mkdir()
    (backend / "demo" / "__pycache__" / "x.pyc").write_bytes(b"x")
    builder = SafeSnapshotBuilder(frontend, backend)
    one = builder.build("demo", tmp_path / "one")
    two = builder.build("demo", tmp_path / "two")
    assert one.input_sha256 == two.input_sha256
    assert not any("__pycache__" in item.path for item in one.files)


def test_snapshot_applies_configured_entry_exclusions(tmp_path):
    frontend, backend = roots(tmp_path)
    (frontend / "demo" / "generated").mkdir()
    (frontend / "demo" / "generated" / "ignored.js").write_text("secret generated output")
    result = SafeSnapshotBuilder(frontend, backend, extra_exclusions=("generated",)).build("demo", tmp_path / "snapshot")
    assert not any("generated" in item.path for item in result.files)


def test_snapshot_rejects_replacement_between_validation_and_descriptor_open(tmp_path, monkeypatch):
    frontend, backend = roots(tmp_path)
    source = frontend / "demo" / "Demo.vue"
    outside = tmp_path / "outside.vue"
    outside.write_bytes(b"external replacement")
    original_stat = source.stat()
    os.utime(outside, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns + 10_000_000))
    real_open = StableRegularFile.open
    replaced = False

    @contextmanager
    def replace_then_open(stable_file):
        nonlocal replaced
        if stable_file.path == source and not replaced:
            replaced = True
            source.rename(source.with_name("Demo.vue.original"))
            os.link(outside, source)
        with real_open(stable_file) as opened:
            yield opened

    monkeypatch.setattr(StableRegularFile, "open", replace_then_open)
    with pytest.raises(PublicError) as error:
        SafeSnapshotBuilder(frontend, backend).build("demo", tmp_path / "snapshot")
    assert error.value.code == "source_changed"


def _replace_directory_with_link(source: Path, original: Path, outside: Path) -> bool:
    try:
        source.rename(original)
    except OSError:
        return False
    if os.name == "nt":
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(source), str(outside)],
            capture_output=True,
            text=True,
        )
        assert created.returncode == 0, created.stderr or created.stdout
    else:
        source.symlink_to(outside, target_is_directory=True)
    return True


def test_snapshot_retains_descendant_parents_during_copy(tmp_path, monkeypatch):
    frontend, backend = roots(tmp_path)
    nested = frontend / "demo" / "nested"
    nested.mkdir()
    source = nested / "secret.txt"
    source.write_bytes(b"inside-good")
    outside = tmp_path / "outside"
    outside.mkdir()
    external = outside / "secret.txt"
    external.write_bytes(b"outside-bad")
    source_stat = source.stat()
    os.utime(external, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
    original = nested.with_name("nested-original")
    real_open = StableRegularFile.open
    attempted = False
    swapped = False

    @contextmanager
    def swap_parent_then_open(stable_file):
        nonlocal attempted, swapped
        if stable_file.path == source and not attempted:
            attempted = True
            swapped = _replace_directory_with_link(nested, original, outside)
        with real_open(stable_file) as opened:
            yield opened

    monkeypatch.setattr(StableRegularFile, "open", swap_parent_then_open)
    if os.name == "nt":
        result = SafeSnapshotBuilder(frontend, backend).build("demo", tmp_path / "snapshot")
        copied = result.root / "frontend" / "src" / "projects" / "demo" / "nested" / "secret.txt"
        assert attempted and not swapped
        assert copied.read_bytes() == b"inside-good"
    else:
        with pytest.raises(PublicError) as error:
            SafeSnapshotBuilder(frontend, backend).build("demo", tmp_path / "snapshot")
        assert attempted and swapped
        assert error.value.code == "source_changed"


@pytest.mark.parametrize(
    "name",
    (".envrc", ".ENV.production", "ＳＥＲＶＥＲ.CRT", "client.cer", "chain.der", "credentials-backup"),
)
def test_snapshot_rejects_sensitive_names_case_insensitively_and_after_nfkc(tmp_path, name):
    frontend, backend = roots(tmp_path)
    (frontend / "demo" / name).write_text("sensitive")
    with pytest.raises(PublicError) as error:
        SafeSnapshotBuilder(frontend, backend).build("demo", tmp_path / "snapshot")
    assert error.value.code == "forbidden_source_file"


def test_snapshot_rejects_secrets(tmp_path):
    frontend, backend = roots(tmp_path)
    (frontend / "demo" / ".ENV.LOCAL").write_text("SECRET=x")
    with pytest.raises(PublicError) as error:
        SafeSnapshotBuilder(frontend, backend).build("demo", tmp_path / "snapshot")
    assert error.value.code == "forbidden_source_file"
