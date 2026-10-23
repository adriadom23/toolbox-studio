from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from app.submissions.git_metadata import LocalGitMetadataProvider


def test_git_metadata_is_sanitized():
    metadata = LocalGitMetadataProvider().inspect(BACKEND.parent)
    assert metadata.commit is None or 7 <= len(metadata.commit) <= 64
    assert metadata.remote is None or "@" not in metadata.remote
