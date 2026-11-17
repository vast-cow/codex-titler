import argparse
import asyncio
import logging
import signal

import structlog

from .config import Settings
from .daemon import scan_once, serve
from .state import State


async def run(settings, *, once=False):
    state = State()
    task = None
    signals = []
    loop = asyncio.get_running_loop()
    try:
        await state.open(settings.database)
        task = asyncio.create_task(scan_once(settings, state) if once else serve(settings, state))
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, task.cancel)
            signals.append(sig)
        return await task
    except asyncio.CancelledError:
        return 130 if once else 0
    finally:
        for sig in signals:
            loop.remove_signal_handler(sig)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if hasattr(state, "db"):
            await state.close()


def main():
    parser = argparse.ArgumentParser(
        description="Automatically repair long Codex thread names",
        epilog="Default: run continuously. Use scan --once for a finite scan (exit 0 on success, "
        "1 on errors, 130 on interruption).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="Generate and log candidates without renaming",
    )
    commands = parser.add_subparsers(dest="command")
    scan = commands.add_parser(
        "scan", help="Scan existing threads and exit after all work finishes"
    )
    scan.add_argument("--once", action="store_true", required=True, help="Perform one full scan")
    scan.add_argument(
        "--dry-run",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Generate and log candidates without renaming",
    )
    args = parser.parse_args()
    settings = Settings(**({"dry_run": args.dry_run} if args.dry_run is not None else {}))
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, settings.log_level)),
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
    )
    try:
        code = asyncio.run(run(settings, once=args.command == "scan"))
    except Exception as exc:
        structlog.get_logger().error("startup_error", error_type=type(exc).__name__)
        code = 1
    raise SystemExit(code or 0)
