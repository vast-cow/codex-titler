import argparse
import asyncio
import logging
import signal

import structlog

from .config import Settings
from .daemon import serve
from .state import State


async def run(settings):
    state = await State().open(settings.database)
    task = asyncio.create_task(serve(settings, state))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        await state.close()


def main():
    parser = argparse.ArgumentParser(description="Automatically repair long Codex thread names")
    parser.add_argument("--dry-run", action="store_true", default=None)
    args = parser.parse_args()
    settings = Settings(**({"dry_run": args.dry_run} if args.dry_run is not None else {}))
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, settings.log_level)),
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
    )
    asyncio.run(run(settings))
