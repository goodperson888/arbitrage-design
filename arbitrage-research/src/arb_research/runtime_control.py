"""Graceful shutdown for console signals and a Windows background worker."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import signal


@asynccontextmanager
async def shutdown_control(request_stop, stop_file: Path | None = None):
    loop = asyncio.get_running_loop()
    previous = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous[sig] = signal.getsignal(sig)
        try:
            loop.add_signal_handler(sig, request_stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(request_stop))

    async def watch():
        while True:
            if stop_file is not None and stop_file.exists():
                request_stop()
                return
            await asyncio.sleep(0.2)

    task = asyncio.create_task(watch()) if stop_file is not None else None
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for sig, handler in previous.items():
            try:
                loop.remove_signal_handler(sig)
            except NotImplementedError:
                pass
            signal.signal(sig, handler)
