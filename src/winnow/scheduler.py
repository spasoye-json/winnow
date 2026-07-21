import asyncio
import time
from datetime import UTC, datetime, timedelta

from winnow import scoring, transcript
from winnow.ingest import LAST_INGEST_KEY, run_ingest
from winnow.scoring import DAILY_CAP, day_count, run_scoring

TICK_INTERVAL = timedelta(minutes=5)
INGEST_INTERVAL = timedelta(hours=6)


def ingest_due(conn, now):
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (LAST_INGEST_KEY,)
    ).fetchone()
    if row is None or row[0] is None:
        return True
    return now - datetime.fromisoformat(row[0]) >= INGEST_INTERVAL


def scoring_due(conn, now):
    if day_count(conn, now.isoformat()) >= DAILY_CAP:
        return False
    row = conn.execute(
        "SELECT 1 FROM videos WHERE transcript_status = 'pending' LIMIT 1"
    ).fetchone()
    return row is not None


def tick(conn, client, now=None, fetch_transcript=None, llm=None, model=None,
         sleep=time.sleep):
    now = now or datetime.now(UTC)
    if ingest_due(conn, now):
        run_ingest(conn, client, now=now.isoformat())
    if scoring_due(conn, now):
        run_scoring(
            conn,
            fetch_transcript or transcript.fetch_transcript,
            llm or scoring.build_client(),
            model or scoring.model_name(),
            now=now.isoformat(),
            sleep=sleep,
        )


async def due_check_loop(on_tick, stop, interval=TICK_INTERVAL):
    while not stop.is_set():
        await on_tick()
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval.total_seconds())
        except TimeoutError:
            pass
