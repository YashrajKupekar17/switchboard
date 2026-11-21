"""The connection to providers: httpx's interface, aiohttp underneath. Decisions: DECISIONS.md Q3.

httpcore's connection pool spends O(n^2) CPU in the number of open connections. Under load it was 88% of the
gateway's CPU, and capped one copy at ~175 RPS. aiohttp's pool doesn't have that problem.

Only the transport changes: upstream.py still calls httpx and sees httpx's exceptions, mapped here so each
failure kind keeps its meaning (DESIGN.md section 4). The connection limit is our own semaphore, not aiohttp's,
so a full pool is always httpx.PoolTimeout (our overload, Q2) and never confused with a provider that won't connect.
Based on httpx-aiohttp (github.com/karpetrosyan/httpx-aiohttp), whose mapping turns both of those, and a dropped
connection after sending, into ConnectTimeout.

Each provider also gets at most `per_provider` of the connections (a bulkhead, DECISIONS.md R2). A provider that
turns slow holds its connections for as long as it's slow; without this it took all of them, and requests to
healthy providers got 503 (R1).
"""
import asyncio
from typing import AsyncIterator, Callable, Optional

import aiohttp
import httpx

# Close idle connections after this long. Under most servers' own keep-alive (uvicorn's is 5 s), so we don't
# send a request down a connection the server is closing at the same moment.
KEEPALIVE_S = 4.0


class ProviderFull(httpx.PoolTimeout):
    """This provider already has its share of our connections. Raised at once, without waiting: the caller should
    try another provider. Our limit, like PoolTimeout, so it says nothing about the provider's health."""


def _to_httpx(exc: aiohttp.ClientError) -> httpx.TransportError:
    """aiohttp's error as the httpx error upstream.classify_exception expects. Order matters: most specific first."""
    if isinstance(exc, aiohttp.ConnectionTimeoutError):
        return httpx.ConnectTimeout(str(exc))  # never connected: nothing was sent
    if isinstance(exc, aiohttp.ClientConnectorError):
        return httpx.ConnectError(str(exc))  # refused, DNS, TLS: nothing was sent
    if isinstance(exc, aiohttp.ServerTimeoutError):
        return httpx.ReadTimeout(str(exc))  # connected and sent, then the provider went quiet
    return httpx.ReadError(str(exc))  # e.g. disconnected after sending: it may have been processed


class _Body(httpx.AsyncByteStream):
    def __init__(self, response: aiohttp.ClientResponse, done: Callable[[], None]):
        self.response = response
        self.done = done  # frees our connection slot; called exactly once

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self.response.content.iter_any():  # whatever has arrived: a stream's chunks aren't held back
                yield chunk
        except aiohttp.ClientError as exc:
            raise _to_httpx(exc) from exc

    async def aclose(self) -> None:
        self.response.release()  # back to aiohttp's pool if it was read to the end, else closed
        self.done()


class AiohttpTransport(httpx.AsyncBaseTransport):
    def __init__(self, max_connections: int, per_provider: Optional[int] = None):
        self.slots = asyncio.Semaphore(max_connections)
        self.per_provider = per_provider  # None: no limit per provider, only the total
        self.open_to: dict = {}  # (host, port) -> connections in use to it
        self.session: Optional[aiohttp.ClientSession] = None  # made on first use, inside the running event loop

    def _session(self) -> aiohttp.ClientSession:
        if self.session is None:
            connector = aiohttp.TCPConnector(
                limit=0,  # no limit of its own: self.slots is the limit
                keepalive_timeout=KEEPALIVE_S,
                ssl=httpx.create_ssl_context(),  # same certificates as httpx (certifi)
            )
            self.session = aiohttp.ClientSession(connector=connector, auto_decompress=False)  # httpx decompresses
        return self.session

    async def _take_slot(self, wait: Optional[float]) -> None:
        # asyncio.wait, not wait_for: on Python 3.9 wait_for can time out just as the slot is granted, and the slot
        # is lost for good. Here, if the cancel loses that race, the slot is given back.
        take = asyncio.ensure_future(self.slots.acquire())
        try:
            done, _ = await asyncio.wait({take}, timeout=wait)
        except BaseException:  # we were cancelled while waiting
            await self._abandon(take)
            raise
        if not done:
            await self._abandon(take)
            raise httpx.PoolTimeout(f"no free provider connection within {wait}s")

    async def _abandon(self, take: asyncio.Future) -> None:
        take.cancel()
        try:
            await take
        except asyncio.CancelledError:
            return
        self.slots.release()  # it was granted after all

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        timeout = request.extensions.get("timeout", {})
        provider = (request.url.host, request.url.port)
        if self.per_provider and self.open_to.get(provider, 0) >= self.per_provider:
            raise ProviderFull(f"{self.per_provider} connections to {request.url.host} already in use")
        self.open_to[provider] = self.open_to.get(provider, 0) + 1  # counted before any await: no one can slip in

        def release():
            self.open_to[provider] -= 1
            self.slots.release()

        try:
            await self._take_slot(timeout.get("pool"))
        except BaseException:
            self.open_to[provider] -= 1
            raise
        try:
            response = await self._session().request(
                request.method, str(request.url),
                headers=list(request.headers.multi_items()),
                data=request.content or None,
                allow_redirects=False,
                compress=False,
                timeout=aiohttp.ClientTimeout(sock_connect=timeout.get("connect"), sock_read=timeout.get("read")),
            )
        except aiohttp.ClientError as exc:
            release()
            raise _to_httpx(exc) from exc
        except BaseException:
            release()
            raise

        released = False

        def done():
            nonlocal released
            if not released:
                released = True
                release()

        return httpx.Response(
            status_code=response.status,
            headers=response.raw_headers,
            stream=_Body(response, done),
            request=request,
            extensions={"http_version": b"HTTP/1.1"},
        )

    async def aclose(self) -> None:
        if self.session is not None:
            await self.session.close()
