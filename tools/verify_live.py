"""Opt-in live smoke test: creates and names ONE synthetic persistent source thread.

Run only against the intended App Server. Helpers are ephemeral; the named source
is retained so its name can be inspected in the UI. No existing thread is mutated.
"""

import argparse
import asyncio
import json
import logging
import re
import time

from codex_auto_title.config import MODEL, Settings
from codex_auto_title.daemon import Monitor
from codex_auto_title.rpc import RPC, connection

JAPANESE = (
    """ログ出力が多いPython処理の速度を改善したいです。
原因の切り分けと計測方法を相談します。この検証では外部操作せず
「確認しました」とだけ返答してください。
```python
"""
    + "\n".join(
        f'logger.info("Processing batch {n}: waiting for worker completion")'
        for n in range(40)
    )
    + "\n```"
)


async def verify(settings):
    monitor = Monitor(settings)
    async with (
        connection(settings) as ws,
        RPC(ws, settings.request_timeout) as observer,
        connection(settings) as source_ws,
        RPC(source_ws, settings.request_timeout) as source,
    ):
        server = await source.initialize()
        account = await source.request("account/read", {"refreshToken": False})
        if account.get("account") is None:
            raise RuntimeError("server is not authenticated")
        session = asyncio.create_task(monitor.session(observer))
        try:
            async with asyncio.timeout(240):
                while not monitor.baselined:
                    if session.done():
                        session.result()
                    await asyncio.sleep(0.1)
                # Candidate policy excludes the monitor's partial startup second.
                await asyncio.sleep(max(0, monitor.since + 0.1 - time.time()))
                params = monitor.generator.start_params()
                params["ephemeral"] = False
                params["config"].update(
                    await monitor.generator.configure_capabilities()
                )
                created = await source.request("thread/start", params)
                tid = created["thread"]["id"]
                while tid not in monitor.candidates:
                    await asyncio.sleep(0.1)
                # Let the ordinary candidate worker observe and defer empty history.
                await asyncio.sleep(settings.poll_seconds * 3)
                candidate = monitor.candidates[tid]
                assert candidate.snapshot is None and not candidate.done
                await source.request(
                    "turn/start",
                    {
                        "threadId": tid,
                        "model": MODEL,
                        "effort": "low",
                        "input": [{"type": "text", "text": JAPANESE}],
                    },
                )
                while not candidate.done:
                    if session.done():
                        session.result()
                    await asyncio.sleep(0.25)
                readback = await source.request(
                    "thread/read", {"threadId": tid, "includeTurns": False}
                )
                name = readback["thread"]["name"]
                assert name == candidate.title and isinstance(name, str)
                assert re.search(r"[\u3040-\u30ff\u3400-\u9fff]", name), name
                assert candidate.snapshot is not None
                result = {
                    "endpoint": settings.endpoint,
                    "server": server["userAgent"],
                    "authenticated": True,
                    "source_thread": tid,
                    "snapshot_sha256": candidate.snapshot.sha256,
                    "discovery": monitor.generator.last_discovery,
                    "title": name,
                    "japanese_script_check": True,
                    "readback_matches": True,
                }
                print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            session.cancel()
            await asyncio.gather(session, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--token-env")
    parser.add_argument("--provider", default="openai")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(verify(Settings(**vars(args), poll_seconds=0.25)))


if __name__ == "__main__":
    main()
