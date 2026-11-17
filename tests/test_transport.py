import asyncio
import json
import socket

import pytest
from websockets.asyncio.server import serve, unix_serve

from codex_auto_title.config import Settings
from codex_auto_title.rpc import RPC, Disconnected, RpcError, connection


@pytest.mark.parametrize("unix", [False, True])
async def test_websocket_handshake_and_dispatch(tmp_path, unix):
    if unix and not hasattr(socket, "AF_UNIX"):
        pytest.skip("AF_UNIX unavailable")
    handshakes, received = [], []

    async def server(ws):
        handshakes.append(ws.request)
        async for raw in ws:
            m = json.loads(raw)
            received.append(m)
            if m.get("method") == "initialize":
                await ws.send(
                    json.dumps(
                        {
                            "method": "thread/started",
                            "params": {"thread": {"id": "early"}},
                        }
                    )
                )
                await ws.send(
                    json.dumps({"id": m["id"], "result": {"userAgent": "test"}})
                )
            elif m.get("method") == "echo":
                await ws.send(
                    json.dumps({"id": 900, "method": "item/tool/call", "params": {}})
                )
                await ws.send(json.dumps({"id": m["id"], "result": m["params"]}))
            elif m.get("method") == "drop":
                await ws.close()

    path = str(tmp_path / "server.sock")
    listener = unix_serve(server, path) if unix else serve(server, "127.0.0.1", 0)
    async with listener as srv:
        endpoint = (
            "unix://" + path
            if unix
            else f"ws://127.0.0.1:{srv.sockets[0].getsockname()[1]}"
        )
        s = Settings(endpoint=endpoint)
        async with connection(s) as ws, RPC(ws, 0.2) as rpc:
            events = []
            rpc.listeners.add(lambda m, p: events.append((m, p)))
            await rpc.initialize()
            assert events[0][1]["thread"]["id"] == "early"
            assert await rpc.request("echo", {"value": 1}) == {"value": 1}
            await asyncio.sleep(0.01)
            assert any(m.get("id") == 900 and "error" in m for m in received)
            with pytest.raises(TimeoutError):
                await rpc.request("no-response")
            assert not rpc.pending
            with pytest.raises(Disconnected):
                await rpc.request("drop")
    assert handshakes[0].headers["Upgrade"].lower() == "websocket"
    assert handshakes[0].path == "/"


async def test_missing_socket(tmp_path):
    with pytest.raises(RpcError, match="socket missing"):
        async with connection(Settings(endpoint="unix://" + str(tmp_path / "missing"))):
            pass
