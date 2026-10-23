from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from app.core.errors import PublicError
from app.submissions.database import connect, transaction
from app.submissions.idempotency import reserve
from app.submissions.migrations import migrate


def test_idempotency_replays_and_rejects_hash_mismatch(tmp_path):
    database = tmp_path / "db.sqlite3"
    migrate(database)
    connection = connect(database)
    with transaction(connection):
        first = reserve(connection, scope="s", actor_id="a", route_template="r", resource_id="demo", key="abcdefgh", request_sha256="one", operation_id="op_one")
    with transaction(connection):
        second = reserve(connection, scope="s", actor_id="a", route_template="r", resource_id="demo", key="abcdefgh", request_sha256="one", operation_id="op_two")
    assert first.kind == "new" and second.operation_id == "op_one"
    with pytest.raises(PublicError):
        with transaction(connection):
            reserve(connection, scope="s", actor_id="a", route_template="r", resource_id="demo", key="abcdefgh", request_sha256="two", operation_id="op_three")
    connection.close()


def test_concurrent_idempotency_reserves_one_operation(tmp_path):
    database = tmp_path / "concurrent.sqlite3"
    migrate(database)
    barrier = threading.Barrier(4)
    operations: list[str] = []
    errors: list[Exception] = []

    def worker(index: int) -> None:
        connection = connect(database)
        try:
            barrier.wait()
            with transaction(connection):
                result = reserve(connection, scope="s", actor_id="a", route_template="r", resource_id="demo", key="concurrent-key", request_sha256="same", operation_id=f"op_{index}")
            operations.append(result.operation_id)
        except Exception as exc:
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert not errors
    assert len(set(operations)) == 1
