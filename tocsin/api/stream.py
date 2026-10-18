from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from tocsin.events import stream_events

router = APIRouter(tags=["stream"])


# Server-Sent Events for the dashboard. Needs an API key like the rest of
# /api; note that the browser's EventSource cannot send headers, so the
# dashboard reads this stream with fetch.
@router.get("/stream")
async def stream(request: Request) -> StreamingResponse:
    return StreamingResponse(
        stream_events(request.app.state.redis, request.is_disconnected),
        media_type="text/event-stream",
        # Tell proxies (nginx, Caddy) not to buffer or cache the stream.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
