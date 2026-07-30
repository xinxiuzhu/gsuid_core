import asyncio
import sqlite3
import threading

from sqlalchemy import text
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from gsuid_core.utils.database import base_models


def test_cross_loop_write_lock_is_reentrant():
    lock = base_models._CrossLoopAsyncLock()

    async def acquire_twice():
        async with lock:
            async with lock:
                return True

    assert asyncio.run(asyncio.wait_for(acquire_twice(), timeout=1))


def test_with_session_serializes_sqlite_writes_across_event_loops(tmp_path, monkeypatch):
    database_path = tmp_path / "concurrency.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        poolclass=NullPool,
        connect_args={"timeout": 0.05},
    )

    async def setup_database():
        async with engine.begin() as connection:
            await connection.exec_driver_sql("CREATE TABLE counter (id INTEGER PRIMARY KEY, value INTEGER NOT NULL)")
            await connection.exec_driver_sql("INSERT INTO counter (id, value) VALUES (1, 0)")

    asyncio.run(setup_database())
    monkeypatch.setattr(
        base_models,
        "async_maker",
        async_sessionmaker(engine, expire_on_commit=False, close_resets_only=False),
    )
    monkeypatch.setattr(base_models, "sqlite_semaphore", base_models._CrossLoopAsyncSemaphore(8))
    monkeypatch.setattr(base_models, "sqlite_write_lock", base_models._CrossLoopAsyncLock())

    class CounterWriter:
        @classmethod
        @base_models.with_session(write=True)
        async def increment(cls, session):
            result = await session.execute(text("SELECT value FROM counter WHERE id = 1"))
            value = result.scalar_one()
            await asyncio.sleep(0.002)
            await session.execute(
                text("UPDATE counter SET value = :value WHERE id = 1"),
                {"value": value + 1},
            )

    async def run_writes():
        for _ in range(5):
            await CounterWriter.increment()

    threads = [threading.Thread(target=lambda: asyncio.run(run_writes())) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    with sqlite3.connect(database_path) as connection:
        value = connection.execute("SELECT value FROM counter WHERE id = 1").fetchone()[0]
    assert value == 40

    asyncio.run(engine.dispose())
