import asyncio
import threading
import time
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.api.api_runtime import run_blocking_api_call, run_db_read
from backend.configs import API_DB_EXECUTOR_WORKERS

@pytest.mark.asyncio
async def test_run_blocking_api_call_success():
    def simple_sync_fn(x, y):
        return x + y

    res = await run_blocking_api_call("test_success", simple_sync_fn, 5, 7)
    assert res == 12

@pytest.mark.asyncio
async def test_run_blocking_api_call_timeout():
    def hanging_fn():
        time.sleep(5)
        
    start_time = time.time()
    try:
        await run_blocking_api_call("test_timeout", hanging_fn, timeout_seconds=0.5)
        pytest.fail("Expected HTTP 503 Timeout")
    except HTTPException as e:
        assert e.status_code == 503
        assert "timeout" in e.detail.lower()
    
    elapsed = time.time() - start_time
    assert elapsed < 1.0  # Should timeout quickly, not hang the event loop

@pytest.mark.asyncio
async def test_api_executor_saturation():
    # We will saturate the executor and then make sure the next call gets 503 quickly
    events = [threading.Event() for _ in range(API_DB_EXECUTOR_WORKERS)]
    
    def hanging_slot_fn(idx):
        events[idx].wait()
    
    # Fill the executor
    tasks = []
    for i in range(API_DB_EXECUTOR_WORKERS):
        # We don't await them yet, so they all start pulling from the semaphore
        t = asyncio.create_task(run_blocking_api_call(f"fill_{i}", hanging_slot_fn, i, timeout_seconds=5))
        tasks.append(t)
        
    # Wait a tiny bit for threads to actually start
    await asyncio.sleep(0.5)
    
    # Now the executor is full. A new request should hit the semaphore acquire timeout
    start_time = time.time()
    try:
        await run_blocking_api_call("test_saturated", lambda: "should not run", timeout_seconds=5)
        pytest.fail("Expected HTTP 503 Retry-After")
    except HTTPException as e:
        assert e.status_code == 503
        assert "high load" in e.detail.lower()
        
    elapsed = time.time() - start_time
    assert elapsed < 0.5  # Semaphore wait is 0.1s
    
    # Clean up the hanging threads
    for e in events:
        e.set()
    
    # wait for tasks to finish
    await asyncio.gather(*tasks)

@pytest.mark.asyncio
async def test_event_loop_responsiveness():
    # To prove we aren't blocking the event loop:
    # Run a blocked API call alongside an asyncio.sleep call
    
    def slow_fn():
        time.sleep(1.0)
    
    async def fast_async_fn():
        await asyncio.sleep(0.1)
        return "fast"
        
    results = await asyncio.gather(
        run_blocking_api_call("slow", slow_fn),
        fast_async_fn()
    )
    assert results[1] == "fast"
