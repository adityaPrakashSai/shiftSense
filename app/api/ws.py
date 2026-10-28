"""WebSocket hub (design doc 10.2): small "something changed" nudges per ward.

Views always refetch over REST, so a dropped socket never leaves a view wrong. `notify` is safe to
call from solver worker threads.
"""

import asyncio
import contextlib
import time
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect


class Hub:
    def __init__(self) -> None:
        self.wards: dict[str, set[WebSocket]] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self.off_until = 0.0  # dev switch: refuse sockets until then (polling fallback check)

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    async def broadcast(self, ward_id: str, msg: dict[str, Any]) -> None:
        dead = []
        for ws in list(self.wards.get(ward_id, ())):
            try:
                await ws.send_json(msg)
            except Exception:  # noqa: BLE001 - a broken socket is just dropped
                dead.append(ws)
        for ws in dead:
            self.wards.get(ward_id, set()).discard(ws)

    def notify(self, ward_id: str, msg: dict[str, Any]) -> None:
        """Thread-safe broadcast; a no-op before startup."""
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            loop.create_task(self.broadcast(ward_id, msg))
        else:
            asyncio.run_coroutine_threadsafe(self.broadcast(ward_id, msg), loop)

    async def disable(self, seconds: float) -> int:
        """Close every socket and refuse new ones for `seconds` (manual polling-fallback test)."""
        self.off_until = time.monotonic() + seconds
        n = 0
        for sockets in self.wards.values():
            for ws in list(sockets):
                n += 1
                with contextlib.suppress(Exception):
                    await ws.close(code=1012)
            sockets.clear()
        return n


hub = Hub()
router = APIRouter()


@router.websocket("/ws/ward/{ward_id}")
async def ward_socket(ws: WebSocket, ward_id: str) -> None:
    if time.monotonic() < hub.off_until:
        await ws.close(code=1013)
        return
    await ws.accept()
    hub.wards.setdefault(ward_id, set()).add(ws)
    try:
        while True:  # the client only sends pings
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        hub.wards.get(ward_id, set()).discard(ws)
