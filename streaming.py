"""Explicit ownership and cancellation-safe cleanup for async response streams."""

import logging

import anyio
from starlette.responses import StreamingResponse


logger = logging.getLogger(__name__)


async def close_stream(stream) -> None:
    close = getattr(stream, "aclose", None)
    if close is None:
        return
    # Keep cleanup in the calling task: ContextVar tokens and some upstream
    # context managers cannot be moved to a new task just to shield them.
    with anyio.move_on_after(5, shield=True) as scope:
        try:
            await close()
        except Exception:
            logger.warning("Failed to close response stream", exc_info=True)
    if scope.cancel_called:
        logger.warning("Timed out closing response stream")


class ClosingStream:
    """Close an iterator and its upstream, even before the first iteration.

    Async generators do not run their finally blocks when closed before they
    start. Keeping ownership here also covers disconnects before the first byte.
    """

    def __init__(self, iterator, *, upstream=None, on_close=None):
        self._iterator = iterator.__aiter__()
        self._upstream = upstream
        self._on_close = on_close
        self._closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._closed:
            raise StopAsyncIteration
        try:
            return await self._iterator.__anext__()
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        try:
            await close_stream(self._iterator)
        finally:
            if self._on_close is not None:
                await self._on_close()
            elif self._upstream is not None:
                await close_stream(self._upstream)


class ClosingStreamingResponse(StreamingResponse):
    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await close_stream(self.body_iterator)
