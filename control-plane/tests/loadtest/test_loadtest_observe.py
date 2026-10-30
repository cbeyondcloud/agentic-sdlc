"""The observers never hang the load test (M3-22): a stress run's driver waited for ever at
the end on a database connection that would not close (asyncpg closes without a limit)."""

import asyncio

import pytest
from cp_loadtest import observe
from cp_loadtest.observe import HeadWatcher, memory_mib
from cp_loadtest.stats import Recorder


class StuckConnection:
    def __init__(self) -> None:
        self.terminated = False

    async def close(self) -> None:
        await asyncio.Event().wait()  # never answers

    def terminate(self) -> None:
        self.terminated = True


def test_a_connection_that_will_not_close_is_terminated(monkeypatch):
    monkeypatch.setattr(observe, "DB_TIMEOUT_S", 0.2)
    watcher = HeadWatcher("postgresql://u:p@127.0.0.1:1/db", Recorder())
    stuck = StuckConnection()
    watcher._conn = stuck

    async def stop():
        await asyncio.wait_for(watcher.stop(), 5)

    asyncio.run(stop())
    assert stuck.terminated


@pytest.mark.parametrize(
    ("text", "mib"),
    [("123.4MiB", 123.4), ("1GiB", 1024.0), ("512KiB", 0.5), ("2.5GB", 2384.2), ("x", None)],
)
def test_memory_from_container_stats(text, mib):
    assert memory_mib(text) == mib
