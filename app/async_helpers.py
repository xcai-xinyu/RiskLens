"""Bounded worker operations with timer-driven completion checks in restricted WSL."""
import asyncio
from concurrent.futures import ThreadPoolExecutor


async def run_sync(function, *args, **kwargs):
    # An explicit executor avoids the default executor's callback-driven shutdown.
    # Restricted WSL sessions may not wake the selector for thread completion events.
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(function, *args, **kwargs)
        while not future.done():
            await asyncio.sleep(0.01)
        return future.result()
