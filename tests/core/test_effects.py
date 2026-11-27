from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.contracts import DeliveryStatus, Effect, EffectResult
from app.core.effects import EffectConflict, EffectQueue, EffectStateError, enqueue_effect
from app.core.store import Store


class FakeTransport:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = []

    def execute(self, effect):
        self.calls.append(effect.effect_id)
        result = next(self.outcomes)
        if isinstance(result, Exception):
            raise result
        return result


def setup_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "queue.sqlite")
    store.initialize()
    with store._connection(write=True) as db:
        db.execute("INSERT INTO profiles VALUES ('p1', 'UTC', 'ignore')")
    return store


def sample(effect_id="e1", key="key-1"):
    return Effect(effect_id=effect_id, profile_id="p1", key=key, kind="email",
                  target_ref="recipient", payload={"subject": "hello", "to": ["a@example.test"]})


def put(store, effect):
    with store._connection(write=True) as db:
        enqueue_effect(db, effect)


def test_enqueue_is_idempotent_and_rejects_key_reuse(tmp_path):
    store = setup_store(tmp_path)
    put(store, sample())
    put(store, sample())
    with pytest.raises(EffectConflict):
        put(store, sample(effect_id="e2"))
    assert EffectQueue(store).status("e1").attempts == 0


def test_retryable_transport_result_can_be_retried(tmp_path):
    store = setup_store(tmp_path)
    put(store, sample())
    queue = EffectQueue(store)
    transport = FakeTransport([
        EffectResult(status=DeliveryStatus.RETRYABLE, error_code="temporary"),
        EffectResult(status=DeliveryStatus.SENT, external_id="mail-1"),
    ])
    assert queue.run_once(transport) == "e1"
    assert queue.status("e1").status == "retryable"
    queue.retry("e1")
    assert queue.run_once(transport) == "e1"
    assert queue.status("e1").status == "sent"
    assert queue.status("e1").attempts == 2


def test_exception_is_unknown_until_operator_confirms_retry(tmp_path):
    store = setup_store(tmp_path)
    put(store, sample())
    queue = EffectQueue(store)
    assert queue.run_once(FakeTransport([TimeoutError()])) == "e1"
    assert queue.status("e1").status == "unknown"
    with pytest.raises(EffectStateError):
        queue.retry("e1")
    queue.retry("e1", verified_by="test-operator")
    assert queue.status("e1").status == "pending"
    with store._connection() as db:
        assert db.execute("SELECT verified_by FROM effect_retry_audit").fetchone()[0] == "test-operator"


def test_expired_lease_after_restart_becomes_unknown(tmp_path):
    store = setup_store(tmp_path)
    put(store, sample())
    with store._connection(write=True) as db:
        db.execute("UPDATE effects SET status='sending', attempts=1, lease_token='lost', lease_expires_at=0")
    queue = EffectQueue(Store(store.path))
    assert queue.run_once(FakeTransport([])) is None
    assert queue.status("e1").status == "unknown"


def test_concurrent_workers_claim_each_effect_once(tmp_path):
    store = setup_store(tmp_path)
    put(store, sample("e1", "k1"))
    put(store, sample("e2", "k2"))

    class SlowTransport:
        def __init__(self):
            self.calls = []
        def execute(self, effect):
            self.calls.append(effect.effect_id)
            return EffectResult(status=DeliveryStatus.SENT)

    transport = SlowTransport()
    queues = [EffectQueue(Store(store.path)), EffectQueue(Store(store.path))]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda q: q.run_once(transport), queues))
    assert set(results) == {"e1", "e2"}
    assert sorted(transport.calls) == ["e1", "e2"]


def test_retry_after_and_attempt_limit(tmp_path, monkeypatch):
    store = setup_store(tmp_path)
    put(store, sample())
    queue = EffectQueue(store)
    transport = FakeTransport([EffectResult(status=DeliveryStatus.RETRYABLE, retry_after_seconds=60)] * 4)
    now = 100.0
    monkeypatch.setattr("app.core.effects.time", lambda: now)
    assert queue.run_once(transport) == "e1"
    now += 59
    assert queue.run_once(transport) is None
    for _ in range(3):
        now += 61
        assert queue.run_once(transport) == "e1"
    assert queue.status("e1").status == "failed"
    assert queue.status("e1").error_code == "retry_limit_reached"
    assert queue.run_once(transport) is None
