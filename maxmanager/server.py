import asyncio
import random
from contextlib import asynccontextmanager
from loguru import logger
from fastapi import FastAPI

from maxmanager.constants import LOOP_INTERVAL_SECONDS, LOOP_JITTER_SECONDS
from maxmanager.core import choose_credential, read_state

_loop_task: asyncio.Task | None = None
_lock = asyncio.Lock()


async def credential_loop() -> None:
    logger.info("Startup: running initial credential selection (guards bypassed)")
    try:
        async with _lock:
            await asyncio.to_thread(choose_credential, True)
    except Exception:
        logger.exception("Startup choose_credential failed; will retry in loop")
    while True:
        sleep_for = max(60, LOOP_INTERVAL_SECONDS + random.uniform(-LOOP_JITTER_SECONDS, LOOP_JITTER_SECONDS))
        logger.debug(f"Loop sleeping for {sleep_for:.0f}s")
        await asyncio.sleep(sleep_for)
        try:
            async with _lock:
                await asyncio.to_thread(choose_credential, False)
        except Exception:
            logger.exception("choose_credential failed; will retry next cycle")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _loop_task
    _loop_task = asyncio.create_task(credential_loop())
    try:
        yield
    finally:
        if _loop_task:
            _loop_task.cancel()


app = FastAPI(title="maxmanager", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/status")
async def status():
    state = read_state()
    if state is None:
        return {"active_profile": None, "switched_at": None, "last_snapshots": []}
    return state.to_dict()


@app.post("/trigger")
async def trigger():
    logger.info("Manual trigger: POST /trigger called")
    async with _lock:
        result = await asyncio.to_thread(choose_credential, False)
    return result
