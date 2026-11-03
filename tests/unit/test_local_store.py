import time
from concurrent.futures import ThreadPoolExecutor
from app.core.local_store import LocalStore


def test_durable_queue_leases_retry_and_recovery(tmp_path):
    store = LocalStore(tmp_path / "coordination.sqlite3")
    id = store.enqueue("inventory", {"account_id": "a"})
    first = store.claim()
    assert first["id"] == id and first["attempts"] == 1
    assert store.claim() is None
    reopened = LocalStore(store.path)
    assert reopened.claim() is None
    with reopened.connection() as db:
        db.execute("UPDATE tasks SET lease_until=? WHERE id=?", (time.time() - 1, id))
    second = reopened.claim()
    assert second["id"] == id and second["attempts"] == 2
    reopened.finish(id, "temporarily unavailable", retry_after=0)
    assert reopened.claim()["attempts"] == 3
    reopened.finish(id)
    assert reopened.claim() is None


def test_parallel_claim_and_persistent_rate_limit(tmp_path):
    store = LocalStore(tmp_path / "state.sqlite3")
    for i in range(20):
        store.enqueue("inventory", {"account_id": str(i)})
    with ThreadPoolExecutor(max_workers=4) as pool:
        claimed = list(pool.map(lambda _: store.claim(), range(20)))
    assert len({t["id"] for t in claimed}) == 20
    assert [store.counter("login", 60) for _ in range(3)] == [1, 2, 3]
    assert LocalStore(store.path).counter("login", 60) == 4
    assert store.reserve_slot("steam", 1) == 0
    assert store.reserve_slot("steam", 1) > 0
    assert store.put("once", True, 60, nx=True)
    assert not store.put("once", False, 60, nx=True)
    assert store.get("once") is True
