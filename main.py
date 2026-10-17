"""Ingress entrypoint. Streamlit lives on /app; this proxy also serves /health."""

import asyncio
import os
import signal
import subprocess
import sys

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

UPSTREAM = "http://127.0.0.1:8501"
PORT = int(os.environ.get("PORT", "8080"))


def _upstream_path(path: str) -> str:
    if path.startswith("/app"):
        return path
    if path == "/":
        return "/app/"
    return "/app" + path


async def health(request: web.Request) -> web.Response:
    client: ClientSession = request.app["client"]
    try:
        async with client.get(UPSTREAM + "/app/_stcore/health", timeout=ClientTimeout(total=2)) as upstream:
            if upstream.status == 200:
                return web.Response(text="ok")
    except Exception:
        pass
    return web.Response(status=503, text="starting")


async def _pipe(src, dest) -> None:
    try:
        async for msg in src:
            if msg.type == WSMsgType.TEXT:
                await dest.send_str(msg.data)
            elif msg.type == WSMsgType.BINARY:
                await dest.send_bytes(msg.data)
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                await dest.close()
                break
            elif msg.type == WSMsgType.ERROR:
                break
    except Exception:
        return


async def proxy(request: web.Request) -> web.StreamResponse:
    if request.path in ("/health", "/app/health"):
        return await health(request)

    path = _upstream_path(request.rel_url.path)
    query = request.rel_url.query_string
    target = UPSTREAM + path + (("?" + query) if query else "")
    hop = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {"host", "content-length"}
    }

    if request.headers.get("Upgrade", "").lower() == "websocket":
        requested = [
            part.strip()
            for part in request.headers.get("Sec-WebSocket-Protocol", "").split(",")
            if part.strip()
        ]
        client = web.WebSocketResponse(protocols=requested)
        await client.prepare(request)
        session: ClientSession = request.app["client"]
        forward = {
            key: value
            for key, value in hop.items()
            if key.lower()
            not in {
                "upgrade",
                "connection",
                "sec-websocket-key",
                "sec-websocket-version",
                "sec-websocket-extensions",
                "sec-websocket-protocol",
            }
        }
        async with session.ws_connect(
            target.replace("http://", "ws://", 1),
            headers=forward,
            protocols=requested,
        ) as upstream:
            await asyncio.gather(_pipe(client, upstream), _pipe(upstream, client))
        return client

    session = request.app["client"]
    async with session.request(
        request.method,
        target,
        headers=hop,
        data=await request.read(),
        allow_redirects=False,
    ) as upstream:
        body = await upstream.read()
        response = web.Response(status=upstream.status, body=body)
        for key, value in upstream.headers.items():
            if key.lower() not in {"content-encoding", "transfer-encoding", "content-length", "connection"}:
                response.headers[key] = value
        return response


async def _on_startup(app: web.Application) -> None:
    app["client"] = ClientSession()


async def _on_cleanup(app: web.Application) -> None:
    await app["client"].close()
    process = app.get("streamlit")
    if process and process.poll() is None:
        process.terminate()


def _start_streamlit() -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            "app.py",
            "--server.port",
            "8501",
            "--server.address",
            "127.0.0.1",
            "--server.baseUrlPath",
            "app",
            "--server.headless",
            "true",
            "--server.enableCORS",
            "false",
            "--server.enableXsrfProtection",
            "false",
            "--browser.gatherUsageStats",
            "false",
            "--server.fileWatcherType",
            "none",
        ]
    )


def main() -> None:
    streamlit = _start_streamlit()
    app = web.Application(client_max_size=180 * 1024 * 1024)
    app["streamlit"] = streamlit
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    app.router.add_get("/health", health)
    app.router.add_route("*", "/", proxy)
    app.router.add_route("*", "/{tail:.*}", proxy)
    web.run_app(app, host="0.0.0.0", port=PORT, handle_signals=True)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    main()
