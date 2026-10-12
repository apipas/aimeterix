"""The ``aimeterix`` command line."""

from __future__ import annotations

import argparse
import json
import logging
import re
import signal
import sys
from collections.abc import Sequence

from ai_meterix import __version__


def _doctor(args: argparse.Namespace) -> int:
    from ai_meterix.collectors.probe import probe

    from .doctor import render

    report = probe()
    if args.json:
        print(json.dumps(report.to_dict(readings=args.readings), indent=2, default=str))
    else:
        print(render(report))
    return 0 if report.platform.backend else 1


_UNITS = {"s": 1, "m": 60, "h": 3600}
_STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)


def duration(text: str) -> float:
    """Seconds from ``90``, ``90s``, ``10m`` or ``2h``."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*", text)
    if not match:
        raise argparse.ArgumentTypeError(f"not a duration: {text!r} (try 30s, 10m or 2h)")
    return float(match.group(1)) * _UNITS[match.group(2) or "s"]


def _agent(args: argparse.Namespace) -> int:
    from ai_meterix.collectors.agent import Agent, open_backend
    from ai_meterix.core.config import load_config
    from ai_meterix.storage import StorageWriter, open_writer

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config = load_config(args.config)
    interval = args.interval or config.agent.interval_s
    try:
        backend = open_backend(args.project, mock=args.mock, interval_s=interval)
    except RuntimeError as exc:
        print(f"aimeterix agent: {exc}", file=sys.stderr)
        return 1
    url = config.storage.url

    def open_storage() -> StorageWriter:
        if url.startswith("sqlite"):
            return open_writer(url, busy_timeout_ms=config.storage.busy_timeout_ms)
        return open_writer(url)

    try:
        agent = Agent(backend, open_storage, interval_s=interval, burst_s=args.burst or 0.0)
    except ValueError as exc:
        print(f"aimeterix agent: {exc}", file=sys.stderr)
        return 2
    previous = {sig: signal.signal(sig, lambda *_: agent.stop()) for sig in _STOP_SIGNALS}
    burst = f", 1 s burst for {args.burst:.0f} s" if args.burst else ""
    logging.getLogger("ai_meterix.agent").info(
        "agent started: %s backend, every %.0f s%s; Ctrl-C to stop", backend.name, interval, burst
    )
    try:
        agent.run(args.duration)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    print(
        f"agent stopped: {agent.written} rows written, {len(agent.pending)} kept in the buffer"
        + (f", {agent.dropped} dropped" if agent.dropped else "")
    )
    return 0 if not agent.last_error else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aimeterix",
        description="Know what your AI compute costs, what you waste and what next month costs.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="command")

    doctor = commands.add_parser("doctor", help="show what this machine can measure")
    doctor.add_argument("--json", action="store_true", help="print the report as JSON")
    doctor.add_argument(
        "--readings",
        action="store_true",
        help="with --json, include the raw readings (no process names)",
    )
    doctor.set_defaults(func=_doctor)

    agent = commands.add_parser("agent", help="sample this machine until stopped")
    agent.add_argument("--interval", type=duration, help="seconds between samples (1 to 300)")
    agent.add_argument("--burst", type=duration, help="sample every second for this long first")
    agent.add_argument("--for", dest="duration", type=duration, help="stop after this long")
    agent.add_argument("--project", default="default", help="project this machine belongs to")
    agent.add_argument("--mock", action="store_true", help="invent readings, for a demo")
    agent.add_argument("--config", help="path to aimeterix.yaml")
    agent.add_argument("-v", "--verbose", action="store_true", help="log more detail")
    agent.set_defaults(func=_agent)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
