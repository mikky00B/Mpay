"""FastAPI application assembly + background loops.

Runs three in-process background jobs [D8]:
- processor loop: applies pending ChainEvents (step 4)
- sweep loop: expiry + confirmation depth (step 5)
- dispatcher loop: webhook delivery with backoff (step 6)

They are plain asyncio tasks sharing the sync engine (each iteration gets its
own short-lived session via run_in_executor to avoid blocking the loop).
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.db import get_sessionmaker
from app.routes import router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


async def _repeat(interval: float, fn, *args) -> None:
    """Run fn periodically, forever; one crash never kills the loop."""
    while True:
        try:
            await asyncio.get_running_loop().run_in_executor(None, fn, *args)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("background job %s failed", getattr(fn, "__name__", fn))
        await asyncio.sleep(interval)


def _run_processor_tick() -> None:
    from app.processor import run_pending
    from app.webhooks import dispatch_due_deliveries  # noqa: F401  (see dispatcher tick)

    db = get_sessionmaker()()
    try:
        outcomes = run_pending(db)
        for o in outcomes:
            log.info("event %s -> %s", o["event"], o["outcome"])
    finally:
        db.close()


def _run_sweep_tick() -> None:
    from app.confirmations import sweep

    db = get_sessionmaker()()
    try:
        stats = sweep(db)
        if any(stats.values()):
            log.info("sweep: %s", stats)
    finally:
        db.close()


def _run_dispatcher_tick() -> None:
    from app.webhooks import dispatch_due_deliveries

    db = get_sessionmaker()()
    try:
        dispatch_due_deliveries(db)
    finally:
        db.close()


def _run_reconciliation_tick() -> None:
    from app.reconciliation import reconcile

    db = get_sessionmaker()()
    try:
        stats = reconcile(db)
        if any(v for k, v in stats.items() if k != "checked"):
            log.info("reconciliation: %s", stats)
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    tasks = [
        asyncio.create_task(_repeat(s.processor_poll_seconds, _run_processor_tick)),
        asyncio.create_task(_repeat(s.sweep_poll_seconds, _run_sweep_tick)),
        asyncio.create_task(_repeat(s.dispatcher_poll_seconds, _run_dispatcher_tick)),
        asyncio.create_task(_repeat(s.reconciliation_poll_seconds, _run_reconciliation_tick)),
    ]
    log.info("background jobs started (processor/sweep/dispatcher/reconciliation)")
    yield
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    log.info("background jobs stopped")


def create_app() -> FastAPI:
    app = FastAPI(title="Mpay", version="0.1.0", lifespan=lifespan)
    app.include_router(router)

    @app.get("/health")
    def health():
        return {"ok": True}

    return app


app = create_app()
