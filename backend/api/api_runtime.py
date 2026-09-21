import asyncio
from concurrent.futures import ThreadPoolExecutor
from fastapi import HTTPException
import logging

from backend.configs import (
    API_DB_EXECUTOR_WORKERS,
    API_DB_READ_TIMEOUT_SECONDS,
    API_BLOCKING_CALL_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

_api_executor = ThreadPoolExecutor(max_workers=API_DB_EXECUTOR_WORKERS, thread_name_prefix="api_db")
_api_semaphore = asyncio.Semaphore(API_DB_EXECUTOR_WORKERS)


async def run_blocking_api_call(operation: str, fn, *args, timeout_seconds=None, **kwargs):
    if timeout_seconds is None:
        timeout_seconds = API_BLOCKING_CALL_TIMEOUT_SECONDS

    # Admission control: wait a very short time to acquire a slot
    try:
        await asyncio.wait_for(_api_semaphore.acquire(), timeout=0.1)
    except asyncio.TimeoutError:
        logger.warning(f"API executor saturated: {operation}")
        raise HTTPException(
            status_code=503, 
            headers={"Retry-After": "1"}, 
            detail="Service temporarily unavailable (high load)"
        )

    loop = asyncio.get_running_loop()
    
    def wrapped_fn():
        try:
            return fn(*args, **kwargs)
        finally:
            # Release the semaphore exactly when the thread actually returns
            loop.call_soon_threadsafe(_api_semaphore.release)
            
    future = loop.run_in_executor(_api_executor, wrapped_fn)
    
    try:
        # Use asyncio.shield so if the request gets cancelled, 
        # the thread doesn't just get orphaned without us catching it.
        # Actually, wrapped_fn releases it anyway, which shields the semaphore.
        return await asyncio.wait_for(asyncio.shield(future), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        logger.error(f"API blocking call timed out in wrapper: {operation}")
        # Return 503 instead of 500 when the DB call is just too slow
        raise HTTPException(
            status_code=503, 
            detail="Service temporarily unavailable (timeout)"
        )


async def run_db_read(operation: str, fn, *args, **kwargs):
    return await run_blocking_api_call(
        operation,
        fn,
        *args,
        timeout_seconds=API_DB_READ_TIMEOUT_SECONDS,
        **kwargs
    )
