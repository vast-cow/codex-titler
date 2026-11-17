import argparse
import asyncio
import logging
import tomllib
from pathlib import Path

from pydantic import ValidationError

from .backfill import Backfill
from .config import Settings
from .daemon import Monitor


def load(argv=None):
    parser = argparse.ArgumentParser(
        description="Title new threads on an existing Codex App Server"
    )
    parser.add_argument("--config", type=Path, help="TOML file with a [titler] table")
    parser.add_argument("--estimation-policy", choices=["utf8_bytes", "exact_only"])
    for name in ("endpoint", "unix-uri", "token-env", "provider"):
        parser.add_argument("--" + name)
    for name in (
        "workers",
        "max-message-bytes",
        "max-title-length",
        "safety-tokens",
        "output-reserve",
        "context-retries",
        "generation-attempts",
        "transient-failures",
        "max-pages",
    ):
        parser.add_argument("--" + name, type=int)
    for name in (
        "estimate-multiplier",
        "context-fraction",
        "request-timeout",
        "generation-timeout",
        "poll-seconds",
        "reconnect-max",
    ):
        parser.add_argument("--" + name, type=float)
    parser.add_argument(
        "--backfill-once",
        action="store_true",
        default=None,
        help="title eligible existing unarchived threads once, then exit",
    )
    parser.add_argument("--verbose", action="store_true")
    args = vars(parser.parse_args(argv))
    verbose, path = args.pop("verbose"), args.pop("config")
    try:
        config = tomllib.loads(path.read_text())["titler"] if path else {}
        config.update({k: v for k, v in args.items() if v is not None})
        return Settings.model_validate(config), verbose
    except (OSError, KeyError, ValueError, ValidationError) as exc:
        parser.error(str(exc))


def main():
    settings, verbose = load()
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        settings.headers()
        if settings.backfill_once:
            raise SystemExit(asyncio.run(Backfill(settings).run()))
        asyncio.run(Monitor(settings).run())
    except KeyboardInterrupt:
        pass
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
