import asyncio
from datetime import UTC, datetime, timedelta

from winnow.ingest import LAST_INGEST_KEY, run_ingest

TICK_INTERVAL = timedelta(minutes=5)
INGEST_INTERVAL = timedelta(hours=6)


def ingest_due(conn, now):
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (LAST_INGEST_KEY,)
    ).fetchone()
    if row is None or row[0] is None:
        return True
    return now - datetime.fromisoformat(row[0]) >= INGEST_INTERVAL


def tick(conn, client, now=None):
    now = now or datetime.now(UTC)
    if ingest_due(conn, now):
        run_ingest(conn, client, now=now.isoformat())


async def due_check_loop(on_tick, stop, interval=TICK_INTERVAL):
    while not stop.is_set():
        await on_tick()
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval.total_seconds())
        except TimeoutError:
            pass
