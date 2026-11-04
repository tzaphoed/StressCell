"""Verteilt Ereignisse aus den Arbeitsthreads an alle verbundenen GUIs."""

from __future__ import annotations

import asyncio
import json
import logging
import math

log = logging.getLogger("stresscell.hub")


def clean(v):
    """NaN/inf -> None, numpy -> Python, rekursiv. JSON kennt kein NaN."""
    if isinstance(v, dict):
        return {k: clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [clean(x) for x in v]
    if hasattr(v, "tolist"):          # numpy-Array oder -Skalar
        return clean(v.tolist())
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


class Hub:
    def __init__(self):
        self.loop: asyncio.AbstractEventLoop | None = None
        self.queue: asyncio.Queue | None = None
        self.clients: set = set()

    async def start(self):
        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue(maxsize=10000)
        asyncio.create_task(self._broadcast())

    def publish(self, event: dict) -> None:
        """Thread-sicher, blockiert nie."""
        if self.loop is None:
            return
        try:
            self.loop.call_soon_threadsafe(self._put, event)
        except RuntimeError:
            pass   # Loop schon beendet

    def _put(self, event):
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            log.warning("event queue full, dropping %s", event.get("type"))

    async def _broadcast(self):
        while True:
            ev = await self.queue.get()
            if not self.clients:
                continue
            try:
                text = json.dumps(ev, allow_nan=False, default=str)
            except ValueError:
                text = json.dumps(clean(ev), allow_nan=False, default=str)
            dead = []
            for ws in list(self.clients):
                try:
                    await ws.send_text(text)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.clients.discard(ws)
