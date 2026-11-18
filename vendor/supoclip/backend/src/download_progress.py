"""Carry download recovery notices from blocking providers to task progress."""

import asyncio
import threading
import time
from contextvars import ContextVar, copy_context
from enum import Enum
from typing import Any, Awaitable, Callable, Optional, TypeVar

from .utils.async_helpers import run_in_thread


class DownloadRecovery(str, Enum):
    CONNECTION = "Retrying video download with another connection..."
    AUDIO = "Recovering video audio..."
    FULL_VIDEO = "Recovering the full video download..."
    BACKUP = "Using a backup video download option..."
    ANOTHER_BACKUP = "Trying another video download recovery option..."


_listener: ContextVar[Optional[Callable[[DownloadRecovery], None]]] = ContextVar(
    "download_recovery_listener", default=None,
)
_cancelled: ContextVar[Optional[threading.Event]] = ContextVar("download_cancelled", default=None)


class DownloadStopped(BaseException):
    """Escape provider fallback/retry handlers when the user cancels."""


def check_download_cancelled() -> None:
    event = _cancelled.get()
    if event is not None and event.is_set():
        raise DownloadStopped()


def bind_download_reporter():
    """yt-dlp's own fragment threads do not inherit Python ContextVars."""
    context = copy_context()
    return lambda status: context.copy().run(report_download_bytes, status)


def bind_download_cancel_check():
    context = copy_context()
    return lambda: context.copy().run(check_download_cancelled)


def report_download_bytes(status: dict) -> None:
    """Only numeric transfer data is allowed into a public task message."""
    check_download_cancelled()
    downloaded = max(0, int(status.get("downloaded_bytes") or 0)) / 1048576
    total = status.get("total_bytes") or status.get("total_bytes_estimate")
    message = f"Downloading media: {downloaded:.1f} MiB"
    if total:
        message += f" / {'~' if not status.get('total_bytes') else ''}{float(total) / 1048576:.1f} MiB"
    if status.get("speed"):
        message += f" · {float(status['speed']) / 1048576:.1f} MiB/s"
    if status.get("fragment_index") and status.get("fragment_count"):
        message += f" · fragment {int(status['fragment_index'])}/{int(status['fragment_count'])}"
    listener = _listener.get()
    if listener:
        listener(message)
T = TypeVar("T")
ProgressCallback = Callable[[int, str, str], Awaitable[None]]


def report_download_recovery(recovery: DownloadRecovery) -> None:
    listener = _listener.get()
    if listener is not None:
        listener(recovery)


async def run_download_in_thread(
    func: Callable[..., T], *args: Any,
    progress_callback: Optional[ProgressCallback] = None,
    progress: int = 10, should_cancel: Optional[Callable[[], Awaitable[bool]]] = None, **kwargs: Any,
) -> T:
    """Persist notices on the event loop while the provider runs in a thread.

    The listener belongs only to this operation. Callbacks finish before the
    next pipeline stage, and never use an async database session in a thread.
    """
    if should_cancel and await should_cancel():
        raise asyncio.CancelledError()
    if progress_callback is None and should_cancel is None:
        return await run_in_thread(func, *args, **kwargs)

    loop = asyncio.get_running_loop()
    updates: asyncio.Queue[Optional[DownloadRecovery]] = asyncio.Queue()
    closed = False
    stop = threading.Event()
    last_transfer = 0.0

    def send(recovery: DownloadRecovery) -> None:
        nonlocal last_transfer
        if not isinstance(recovery, DownloadRecovery):
            now = time.monotonic()
            if now - last_transfer < 1:
                return
            last_transfer = now
        if not closed:
            loop.call_soon_threadsafe(updates.put_nowait, recovery)

    def execute() -> T:
        token = _listener.set(send)
        cancellation = _cancelled.set(stop)
        try:
            return func(*args, **kwargs)
        finally:
            _listener.reset(token)
            _cancelled.reset(cancellation)

    operation = asyncio.create_task(run_in_thread(execute))
    # Thread notices are queued before the operation's completion callback.
    operation.add_done_callback(lambda _: updates.put_nowait(None))
    previous = None
    try:
        while True:
            if should_cancel and await should_cancel():
                stop.set()
            try:
                recovery = await asyncio.wait_for(updates.get(), timeout=.5)
            except asyncio.TimeoutError:
                continue
            if recovery is None:
                break
            if recovery != previous:
                if progress_callback:
                    await progress_callback(progress, recovery.value if isinstance(recovery, DownloadRecovery) else recovery, "processing")
                previous = recovery
        return await operation
    except DownloadStopped:
        raise asyncio.CancelledError() from None
    finally:
        closed = True
        stop.set()
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
