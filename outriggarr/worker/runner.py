"""Job runner: the background task started by main.py.

M0: the loop exists and shuts down cleanly. Job processing arrives in M2.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from outriggarr.db.session import SessionFactory

log = logging.getLogger(__name__)

POLL_SECONDS = 5.0


async def run_worker(session_factory: SessionFactory, stop: asyncio.Event) -> None:
    log.info("worker started")
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=POLL_SECONDS)
    log.info("worker stopped")
