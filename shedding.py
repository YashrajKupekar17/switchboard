"""Load shedding: when this copy can't keep up, refuse new chat requests at once. Decisions: DECISIONS.md Q5.

Without it, a full copy accepts everything and queues it, so every request gets slow together: cache hits that
take 1 ms took 8.7 s at 1.5x capacity. A fast 503 lets the client retry, likely on another copy, while the
requests already accepted finish in normal time.

The signal is the gateway's own time per request: from our middleware until we call the provider (or serve a
cache hit). It's ~1-3 ms when healthy, and any queue inside this copy shows up in it, whatever the queue is: the
event loop, a lock, a connection pool. Load tests found all three (Q2, Q5). Provider time is left out, so a slow
provider never makes us refuse work we have room for.

Like CoDel (a router's queue algorithm): look at the *minimum* over a short window. A burst makes a few requests
wait, but if even the fastest request in the window waited, there's a standing queue.
"""
import json
import time
from typing import Callable

from metrics import CHAT_PATH, GATEWAY_SECONDS, SHED

WINDOW = 0.1  # seconds


class Overload:
    """Decides, one window at a time, whether this copy is overloaded."""

    def __init__(self, target: float, clock: Callable[[], float] = time.perf_counter):
        self.target = target  # seconds; 0 turns shedding off
        self.clock = clock
        self.window_ends = clock() + WINDOW
        self.window_min = None  # fastest gateway time seen this window
        self.overloaded = False

    def _roll(self, now: float) -> None:
        if now >= self.window_ends:
            # The window that just ended decides the next one. An empty window means nothing was waiting: without
            # this, shedding everything would stop all new samples and shedding would never end.
            self.overloaded = self.window_min is not None and self.window_min > self.target
            self.window_min = None
            self.window_ends = now + WINDOW

    def record(self, seconds: float) -> None:
        GATEWAY_SECONDS.observe(seconds)
        self._roll(self.clock())
        self.window_min = seconds if self.window_min is None else min(self.window_min, seconds)

    def shedding(self) -> bool:
        if not self.target:
            return False
        self._roll(self.clock())
        return self.overloaded


REFUSED = json.dumps({"error": {
    "message": "this gateway copy is at capacity, retry shortly",
    "type": "gateway_overloaded",
}}).encode()


class Shedder:
    """ASGI middleware, outside FastAPI: a refused request is never parsed, so refusing costs far less than serving.
    Every request it lets through gets its arrival time in scope["state"]["arrived"], where the clock starts."""

    def __init__(self, app, overload: Overload):
        self.app = app
        self.overload = overload

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] == CHAT_PATH:
            if self.overload.shedding():
                SHED.inc()
                await send({"type": "http.response.start", "status": 503, "headers": [
                    (b"content-type", b"application/json"), (b"retry-after", b"1"),
                    (b"content-length", str(len(REFUSED)).encode()),
                ]})
                await send({"type": "http.response.body", "body": REFUSED})
                return
            scope.setdefault("state", {})["arrived"] = time.perf_counter()
        await self.app(scope, receive, send)
