"""The aiohttp transport (DECISIONS.md Q3), against real local servers: each failure must reach
upstream.classify_exception as the same kind httpx's own transport gave, and a connection slot must always come back."""
import asyncio
import socket

import httpx
from aiohttp import web

from transport import AiohttpTransport
from upstream import classify_exception

SHORT = httpx.Timeout(connect=1.0, read=0.3, write=1.0, pool=0.2)


async def serve(handler):
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}/v1/chat/completions"


def run(test):
    asyncio.run(test())


def kind(exc):
    return classify_exception(exc).kind


async def ok(request):
    body = await request.json()
    return web.json_response({"echo": body, "auth": request.headers.get("Authorization")})


def test_answers_like_httpx():
    async def test():
        runner, url = await serve(ok)
        async with httpx.AsyncClient(transport=AiohttpTransport(10)) as http:
            resp = await http.post(url, json={"model": "m"}, headers={"Authorization": "Bearer k"})
        await runner.cleanup()
        assert resp.status_code == 200
        assert resp.json() == {"echo": {"model": "m"}, "auth": "Bearer k"}
    run(test)


def test_stream_chunks_arrive_as_they_are_sent():
    async def test():
        release = asyncio.Event()

        async def sse(request):
            resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await resp.prepare(request)
            await resp.write(b"data: first\n\n")
            await release.wait()  # the second chunk only goes once the client has seen the first
            await resp.write(b"data: second\n\n")
            return resp

        runner, url = await serve(sse)
        async with httpx.AsyncClient(transport=AiohttpTransport(10)) as http:
            async with http.stream("POST", url, json={}) as resp:
                lines = resp.aiter_lines()
                assert await asyncio.wait_for(lines.__anext__(), 1) == "data: first"
                release.set()
                rest = [line async for line in lines if line]
        await runner.cleanup()
        assert rest == ["data: second"]
    run(test)


def test_full_pool_is_a_pool_timeout_so_our_overload():
    async def test():
        hold = asyncio.Event()

        async def slow(request):
            await hold.wait()
            return web.json_response({})

        runner, url = await serve(slow)
        async with httpx.AsyncClient(transport=AiohttpTransport(1), timeout=SHORT) as http:
            first = asyncio.ensure_future(http.post(url, json={}, timeout=5))
            await asyncio.sleep(0.05)  # first holds the only slot
            try:
                await http.post(url, json={})
                raise AssertionError("expected PoolTimeout")
            except httpx.PoolTimeout as exc:
                assert kind(exc) == "overloaded"
            hold.set()
            assert (await first).status_code == 200
            assert (await http.post(url, json={})).status_code == 200  # the slot came back
        await runner.cleanup()
    run(test)


def test_refused_connection_is_connect_so_safe_to_retry():
    async def test():
        with socket.socket() as s:  # a port nobody listens on
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        async with httpx.AsyncClient(transport=AiohttpTransport(1), timeout=SHORT) as http:
            for _ in range(3):  # also proves each failure gave its slot back: there's only one
                try:
                    await http.post(f"http://127.0.0.1:{port}/", json={})
                    raise AssertionError("expected ConnectError")
                except httpx.ConnectError as exc:
                    assert kind(exc) == "connect"
    run(test)


def test_silent_provider_is_a_read_timeout():
    async def test():
        async def silent(request):
            await asyncio.sleep(2)
            return web.json_response({})

        runner, url = await serve(silent)
        async with httpx.AsyncClient(transport=AiohttpTransport(1), timeout=SHORT) as http:
            try:
                await http.post(url, json={})
                raise AssertionError("expected ReadTimeout")
            except httpx.ReadTimeout as exc:
                assert kind(exc) == "timeout"
        await runner.cleanup()
    run(test)


def test_dropped_after_sending_is_network_so_never_retried():
    async def test():
        async def drop(reader, writer):
            await reader.read(65536)  # the whole request arrived: the provider may already be working on it
            writer.close()

        server = await asyncio.start_server(drop, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient(transport=AiohttpTransport(1), timeout=SHORT) as http:
            try:
                await http.post(f"http://127.0.0.1:{port}/", json={})
                raise AssertionError("expected ReadError")
            except httpx.ReadError as exc:
                assert kind(exc) == "network"
        server.close()
    run(test)


def test_cancelled_while_waiting_gives_no_slot_away():
    async def test():
        transport = AiohttpTransport(1)
        await transport.slots.acquire()  # the pool is full
        waiter = asyncio.ensure_future(transport._take_slot(5))
        await asyncio.sleep(0.05)
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        transport.slots.release()
        assert not transport.slots.locked()  # exactly one slot, free again
        await transport.aclose()
    run(test)


def test_slot_granted_just_as_we_give_up_is_given_back():
    async def test():
        transport = AiohttpTransport(1)
        take = asyncio.ensure_future(transport.slots.acquire())
        await asyncio.sleep(0)  # granted, but we've already decided to give up (the race on timeout or cancel)
        await transport._abandon(take)
        assert not transport.slots.locked()
        await transport.aclose()
    run(test)
