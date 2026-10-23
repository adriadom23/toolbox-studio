from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.core.security import ReplayGuard, consume_proxy_nonce, proxy_signature, redact, verify_csrf, verify_proxy_request  # noqa: E402
from app.submissions.database import connect  # noqa: E402
from app.submissions.migrations import migrate  # noqa: E402


def test_hmac_verification_and_replay_rejection():
    guard = ReplayGuard(ttl_seconds=300)
    signature = proxy_signature("secret", "alice", "1000", "nonce", "GET", "/api/projects")
    args = dict(secret="secret", identity="alice", timestamp="1000", nonce="nonce", signature=signature, method="GET", path="/api/projects", replay_guard=guard, now=1000)
    assert verify_proxy_request(**args)
    assert not verify_proxy_request(**args)


def test_redaction_and_csrf():
    assert redact({"token": "secret", "safe": [1]}) == {"token": "[REDACTED]", "safe": [1]}
    assert verify_csrf("same", "same")
    assert not verify_csrf("same", "different")


def test_proxy_nonce_retention_prunes_only_expired_rows_and_rejects_concurrent_replay(tmp_path):
    database = tmp_path / "nonces.sqlite3"
    migrate(database)
    now = datetime.now(timezone.utc)
    connection = connect(database)
    connection.execute(
        "INSERT INTO proxy_replay_nonces(nonce_hash,actor_id,request_id,expires_at,created_at) VALUES ('expired','old','old',?,?)",
        ((now - timedelta(minutes=1)).isoformat(), (now - timedelta(minutes=10)).isoformat()),
    )
    connection.execute(
        "INSERT INTO proxy_replay_nonces(nonce_hash,actor_id,request_id,expires_at,created_at) VALUES ('active','active','active',?,?)",
        ((now + timedelta(minutes=5)).isoformat(), now.isoformat()),
    )
    connection.close()

    def consume_once():
        worker = connect(database)
        try:
            return consume_proxy_nonce(
                worker,
                nonce="same-nonce",
                actor="alice",
                request_id="request-id",
                timestamp=now.timestamp(),
            )
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [future.result() for future in (pool.submit(consume_once), pool.submit(consume_once))]
    assert sorted(results) == [False, True]
    connection = connect(database)
    rows = {row[0] for row in connection.execute("SELECT nonce_hash FROM proxy_replay_nonces")}
    connection.close()
    assert "expired" not in rows
    assert "active" in rows
    assert len(rows) == 2
