"""Event bus between the pipeline and every connected browser.

Same shape as the agent_environment world bus: a subscriber gets an asyncio
Queue, the socket sends a snapshot first and then drains the queue. The
difference is that pipeline stages are blocking (httpx, Playwright), so they run
in a worker thread and publish back through the loop.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

MAX_LOG = 400


class Bus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self.history: deque[dict[str, Any]] = deque(maxlen=MAX_LOG)
        # What the pipeline is doing right now, so a browser that connects
        # mid-run sees the run instead of an idle dashboard.
        self.running: str | None = None
        self.progress: dict[str, Any] = {}

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers.discard(queue)

    def publish(self, kind: str, **fields: Any) -> None:
        """Safe to call from any thread."""
        event = {"type": kind, "at": datetime.now(timezone.utc).isoformat(), **fields}
        if kind in {"log", "stage", "job"}:
            self.history.append(event)
        with self._lock:
            targets = list(self._subscribers)
            loop = self._loop
        if loop is None:
            return
        for queue in targets:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            except RuntimeError:
                pass

    # Convenience wrappers so pipeline code reads cleanly.
    def log(self, message: str, level: str = "info", **extra: Any) -> None:
        self.publish("log", message=message, level=level, **extra)

    def stage(self, name: str, state: str, **extra: Any) -> None:
        self.running = name if state == "start" else None
        if state == "start":
            self.progress = {"stage": name, "done": 0, "total": extra.get("total", 0)}
        self.publish("stage", stage=name, state=state, **extra)

    def tick(self, done: int, total: int, label: str = "") -> None:
        self.progress = {"stage": self.running, "done": done, "total": total}
        self.publish("progress", done=done, total=total, label=label)


BUS = Bus()
