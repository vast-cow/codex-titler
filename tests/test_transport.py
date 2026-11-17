import asyncio
import json

from websockets.asyncio.server import serve, unix_serve

from codex_titler.config import Settings
from codex_titler.transport import Rpc


async def handler(ws):
    async for raw in ws:
        msg = json.loads(raw)
        if msg.get("method") == "initialize":
            await ws.send(json.dumps({"id": msg["id"], "result": {"userAgent": "fake/1"}}))
        elif msg.get("method") == "test":
            await ws.send(json.dumps({"method": "unknown/additive", "params": {"new": 1}}))
            await ws.send(json.dumps({"id": "server-request", "method": "tool/call", "params": {}}))
            await ws.send(json.dumps({"id": msg["id"], "result": {"large": "x" * 2000000}}))
        elif msg.get("id") == "server-request":
            assert msg["error"]["code"] == -32601


async def test_websocket_dispatch_large_payload_and_server_request():
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        rpc = Rpc(Settings(context_limit=32000, endpoint=f"ws://127.0.0.1:{port}"))
        events = []
        rpc.listeners.add(lambda method, params: events.append(method))
        await rpc.open()
        assert len((await rpc.call("test", {}))["large"]) == 2000000
        assert events == ["unknown/additive"]
        await rpc.close()


async def test_unix_websocket(tmp_path):
    path = str(tmp_path / "app.sock")
    async with unix_serve(handler, path):
        rpc = Rpc(Settings(context_limit=32000, endpoint="unix://" + path))
        await rpc.open()
        assert rpc.server_info["userAgent"] == "fake/1"
        await rpc.close()


async def test_disconnect_fails_pending():
    async def close(ws):
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("method") == "initialize":
                await ws.send(json.dumps({"id": msg["id"], "result": {}}))
            elif msg.get("method") == "test":
                await ws.close()

    async with serve(close, "127.0.0.1", 0) as server:
        rpc = Rpc(
            Settings(
                context_limit=32000, endpoint=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            )
        )
        await rpc.open()
        try:
            await rpc.call("test", {})
            assert False, "expected connection error"
        except ConnectionError:
            pass
        await asyncio.wait_for(rpc.closed.wait(), 1)
        await rpc.close()
