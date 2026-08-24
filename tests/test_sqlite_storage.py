from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from lipas.calculus import BeliefContext, Claim
from lipas.exceptions import LipasError
from lipas.serialization.store_sqlite import SqliteClaimStore
from lipas.sqlite_storage import (
    SQLitePolicy,
    SQLiteFailureKind,
    classify_sqlite_failure,
    connect_sqlite,
    immediate_transaction,
)


def test_shared_sqlite_policy_enables_wal_and_bounded_wait(tmp_path):
    connection = connect_sqlite(tmp_path / "state.db")
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5_000
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA trusted_schema").fetchone()[0] == 0
    finally:
        connection.close()


def test_immediate_transaction_rolls_back_the_body(tmp_path):
    connection = connect_sqlite(tmp_path / "state.db")
    connection.execute("CREATE TABLE values_table(value INTEGER NOT NULL)")
    with pytest.raises(RuntimeError, match="abort"):
        with immediate_transaction(connection):
            connection.execute("INSERT INTO values_table VALUES(1)")
            raise RuntimeError("abort")
    assert connection.execute("SELECT COUNT(*) FROM values_table").fetchone()[0] == 0
    connection.close()


def test_immediate_transaction_honors_one_busy_timeout_by_default(tmp_path):
    path = tmp_path / "contended.db"
    policy = SQLitePolicy(busy_timeout_ms=25)
    owner = connect_sqlite(path, policy=policy)
    contender = connect_sqlite(path, policy=policy)
    try:
        owner.execute("CREATE TABLE values_table(value INTEGER NOT NULL)")
        owner.commit()
        owner.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            with immediate_transaction(contender):
                pass
        elapsed = time.monotonic() - started
        assert elapsed < 0.08
    finally:
        owner.rollback()
        owner.close()
        contender.close()


def test_normal_durability_is_an_explicit_performance_choice(tmp_path):
    connection = connect_sqlite(
        tmp_path / "fast.db",
        policy=SQLitePolicy(synchronous="NORMAL"),
    )
    try:
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 1
    finally:
        connection.close()


def test_sqlite_failure_classification_is_explicit():
    assert classify_sqlite_failure(
        sqlite3.OperationalError("database is locked"),
    ) is SQLiteFailureKind.BUSY
    assert classify_sqlite_failure(
        sqlite3.OperationalError("database or disk is full"),
    ) is SQLiteFailureKind.DISK_FULL


def test_claim_store_coordinates_concurrent_sqlite_writers(tmp_path):
    path = tmp_path / "claims.db"
    SqliteClaimStore(path).close()

    def append_partition(partition: int) -> None:
        store = SqliteClaimStore(path, snapshot_interval=10_000)
        try:
            for offset in range(25):
                identity = f"claim-{partition}-{offset}"
                store.fold(Claim(
                    tag="observation",
                    fields={"partition": partition, "offset": offset},
                    claim_id=identity,
                ))
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=4) as executor:
        tuple(executor.map(append_partition, range(4)))

    reopened = SqliteClaimStore(path)
    try:
        assert len(reopened) == 100
        assert reopened.seq == 100
        assert {claim.seq for claim in reopened} == set(range(100))
        assert len({claim.claim_id for claim in reopened}) == 100
    finally:
        reopened.close()


def test_claim_store_concurrent_redelivery_is_idempotent(tmp_path):
    path = tmp_path / "claims.db"
    SqliteClaimStore(path).close()
    claim = Claim(
        tag="observation", fields={"value": 1}, claim_id="stable-id",
    )

    def redeliver(_: int) -> None:
        store = SqliteClaimStore(path)
        try:
            store.fold(claim)
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=4) as executor:
        tuple(executor.map(redeliver, range(4)))
    reopened = SqliteClaimStore(path)
    try:
        assert len(reopened) == 1
        assert reopened.log[0].claim_id == "stable-id"
    finally:
        reopened.close()


def test_claim_projection_snapshot_replays_only_the_delta(tmp_path):
    path = tmp_path / "claims.db"
    store = SqliteClaimStore(path, snapshot_interval=10_000)
    for value in range(100):
        store.fold(Claim(
            tag="observation",
            fields={"value": value},
            claim_id=f"claim-{value}",
        ))
    assert store.checkpoint_projection() == 99
    assert store.resident_claim_count == 0
    store.close()

    reopened = SqliteClaimStore(path)
    try:
        assert reopened.snapshot_seq == 99
        assert reopened.resident_claim_count == 0
        assert reopened.merged.fields["value"] == 99
        first = reopened.read_page(limit=17)
        second = reopened.read_page(after_seq=first.next_cursor, limit=17)
        assert len(first.claims) == 17
        assert first.has_more
        assert second.claims[0].seq == 17
        assert len(reopened.log) == 100
    finally:
        reopened.close()


def test_corrupt_projection_snapshot_falls_back_to_claim_tape(tmp_path):
    path = tmp_path / "claims.db"
    store = SqliteClaimStore(path, snapshot_interval=10_000)
    try:
        for value in range(3):
            store.fold(Claim(
                tag="observation",
                fields={"value": value},
                claim_id=f"claim-{value}",
            ))
        assert store.checkpoint_projection() == 2
    finally:
        store.close()

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE claim_projection_snapshots "
            "SET fields_json='not-json',snapshot_seq=999",
        )

    reopened = SqliteClaimStore(path)
    try:
        assert reopened.snapshot_seq == -1
        assert reopened.seq == 3
        assert reopened.merged.fields["value"] == 2
    finally:
        reopened.close()


def test_claim_projection_refuses_changed_reducer_context(tmp_path):
    context = BeliefContext()
    store = SqliteClaimStore(
        tmp_path / "claims.db", ctx=context, snapshot_interval=10_000,
    )
    try:
        store.fold(Claim(tag="observation", fields={"value": 1}))
        context.caution_threshold += 1
        with pytest.raises(LipasError, match="context changed"):
            store.checkpoint_projection()
    finally:
        store.close()


def test_claim_store_rejects_a_non_monotonic_explicit_sequence(tmp_path):
    store = SqliteClaimStore(tmp_path / "claims.db")
    try:
        with pytest.raises(LipasError, match="next durable sequence"):
            store.fold(Claim(tag="observation", fields={}, seq=10))
        assert len(store) == 0
    finally:
        store.close()
