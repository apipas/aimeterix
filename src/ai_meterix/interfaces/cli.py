"""The ``aimeterix`` command line."""

from __future__ import annotations

import argparse
import json
import logging
import re
import signal
import sys
import threading
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta, timezone

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


def _route(text: str) -> tuple[str, str]:
    name, sep, url = text.partition("=")
    if not sep or not name or not url:
        raise argparse.ArgumentTypeError(f"expected NAME=URL, got {text!r}")
    return name.strip(), url.strip()


def _proxy(args: argparse.Namespace) -> int:
    from ai_meterix.collectors.proxy import Recorder, Router, key_hash, serve
    from ai_meterix.core.config import ProxyConfig, load_config
    from ai_meterix.core.errors import ConfigError
    from ai_meterix.storage import StorageWriter, open_writer

    if args.hash_key:
        key = sys.stdin.readline().strip()
        if not key:
            print("aimeterix proxy: paste the API key on standard input", file=sys.stderr)
            return 2
        print(key_hash(key))
        return 0
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        config = load_config(args.config)
        settings = config.proxy.model_dump()
        settings["upstreams"] = {**settings["upstreams"], **dict(args.route or [])}
        if args.upstream:
            settings["default_upstream"] = args.upstream
        if args.port:
            settings["port"] = args.port
        proxy_config = ProxyConfig.model_validate(settings)
    except (ConfigError, ValueError) as exc:
        print(f"aimeterix proxy: {exc}", file=sys.stderr)
        return 2
    url = config.storage.url

    def open_storage() -> StorageWriter:
        if url.startswith("sqlite"):
            return open_writer(url, busy_timeout_ms=config.storage.busy_timeout_ms)
        return open_writer(url)

    router = Router(proxy_config.upstreams, proxy_config.default_upstream)
    recorder = Recorder(open_storage)
    try:
        server = serve(
            router,
            recorder,
            host=proxy_config.host,
            port=proxy_config.port,
            allowed_key_hashes=proxy_config.allowed_key_hashes,
        )
    except (ValueError, OSError) as exc:
        print(f"aimeterix proxy: {exc}", file=sys.stderr)
        return 1
    base = f"http://{proxy_config.host}:{server.server_address[1]}"
    logger = logging.getLogger("ai_meterix.proxy")
    logger.info("proxy on %s; Ctrl-C to stop", base)
    for line in router.describe():
        logger.info("  %s%s", base, line)
    if proxy_config.allowed_key_hashes:
        logger.info("  forwarding only %d allowed API keys", len(proxy_config.allowed_key_hashes))
    stop = threading.Event()
    previous = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in _STOP_SIGNALS}
    recorder.start()
    thread = threading.Thread(target=server.serve_forever, name="aimeterix-proxy", daemon=True)
    thread.start()
    try:
        stop.wait(args.duration)
    finally:
        server.shutdown()
        server.server_close()
        recorder.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    print(
        f"proxy stopped: {recorder.written} calls written, {recorder.pending} kept in the buffer"
        + (f", {recorder.dropped} dropped" if recorder.dropped else "")
    )
    return 0 if not recorder.last_error else 1


def _day(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a date: {text!r} (use YYYY-MM-DD)") from None


def _usage(args: argparse.Namespace) -> int:
    from ai_meterix.core.config import load_config
    from ai_meterix.core.errors import AiMeterixError
    from ai_meterix.pricing.engine import CostEngine, local_zone

    from .usage import render_csv, render_table

    tz = local_zone()
    now = datetime.now(timezone.utc)
    if args.last:
        start, end = now - timedelta(seconds=args.last), now
    else:
        first = args.since or (args.until or now.astimezone(tz).date())
        last = args.until or first
        if last < first:
            print("aimeterix usage: --to is before --from", file=sys.stderr)
            return 2
        start = datetime.combine(first, time.min, tz).astimezone(timezone.utc)
        end = datetime.combine(last + timedelta(days=1), time.min, tz).astimezone(timezone.utc)
    try:
        from ai_meterix.storage import open_reader

        storage = open_reader(args.url or load_config(args.config).storage.url)
    except AiMeterixError as exc:
        print(f"aimeterix usage: {exc}", file=sys.stderr)
        return 1
    try:
        statement = CostEngine(storage, tz).statement(start, min(end, now), now=now)
    finally:
        storage.close()
    if args.csv:
        text = render_csv(statement, by=args.by)
        if args.csv == "-":
            sys.stdout.write(text)
        else:
            with open(args.csv, "w", encoding="utf-8", newline="") as out:
                out.write(text)
            print(f"wrote {args.csv}")
        return 0
    print(render_table(statement, by=args.by))
    return 0


def _profile(args: argparse.Namespace) -> int:
    import getpass
    from decimal import Decimal, InvalidOperation

    from ai_meterix.core.config import load_config
    from ai_meterix.core.errors import AiMeterixError
    from ai_meterix.core.model import CURRENCY, CostProfile, FeeSpread
    from ai_meterix.storage import open_reader, open_writer

    url = args.url or load_config(args.config).storage.url
    try:
        storage = open_writer(url) if args.action == "set" else open_reader(url)
    except AiMeterixError as exc:
        print(f"aimeterix profile: {exc}", file=sys.stderr)
        return 1
    try:
        hosts = [h.host.id for h in storage.get_hosts()]
        host = args.host or (hosts[0] if len(hosts) == 1 else None)
        if host is None:
            known = ", ".join(hosts) or "none yet: run `aimeterix agent` first"
            print(f"aimeterix profile: name the machine with --host ({known})", file=sys.stderr)
            return 2
        current = storage.get_profile(host)
        if args.action == "set":
            try:
                changes = {
                    name: Decimal(value)
                    for name, value in (
                        ("energy_rate_per_kwh", args.rate),
                        ("pue", args.pue),
                        ("fixed_fees_per_month", args.fees),
                    )
                    if value is not None
                }
            except InvalidOperation:
                print("aimeterix profile: --rate, --pue and --fees take numbers", file=sys.stderr)
                return 2
            if current is None and "energy_rate_per_kwh" not in changes:
                print("aimeterix profile: the first profile needs --rate", file=sys.stderr)
                return 2
            if current is not None and current.currency != CURRENCY:
                needed = ["--rate"] + (["--fees"] if current.fixed_fees_per_month else [])
                missing = [n for n in needed if getattr(args, n[2:]) is None]
                if missing:
                    print(
                        f"aimeterix profile: your profile is in {current.currency}, and AI Meterix "
                        f"uses {CURRENCY} for now; give {' and '.join(missing)} in {CURRENCY}",
                        file=sys.stderr,
                    )
                    return 2
            base = current or CostProfile(host, "default", CURRENCY, Decimal(0))
            profile = CostProfile(
                host,
                args.name or base.name,
                CURRENCY,
                changes.get("energy_rate_per_kwh", base.energy_rate_per_kwh),
                region=args.region if args.region is not None else base.region,
                pue=changes.get("pue", base.pue),
                fixed_fees_per_month=changes.get("fixed_fees_per_month", base.fixed_fees_per_month),
                fee_spread=FeeSpread(args.spread) if args.spread else base.fee_spread,
            )
            current = storage.save_profile(profile, by=getpass.getuser())  # type: ignore[attr-defined]
        history = storage.get_profile_history(host)
    except AiMeterixError as exc:
        print(f"aimeterix profile: {exc}", file=sys.stderr)
        return 2
    finally:
        storage.close()
    if current is None:
        print(f"{host}: no cost profile yet; set one with `aimeterix profile set --rate 0.30`")
        return 0
    print(f"{host}: profile {current.name!r}, version {current.version}")
    print(f"  energy   {current.energy_rate_per_kwh} {current.currency}/kWh x PUE {current.pue}")
    print(
        f"  fees     {current.fixed_fees_per_month} {current.currency}/month, "
        f"spread by {current.fee_spread.value}"
    )
    print(f"  region   {current.region or '-'}")
    if args.action == "history":
        for p in history:
            saved = f"{p.saved_at:%Y-%m-%d %H:%M} UTC" if p.saved_at else "-"
            print(
                f"  v{p.version}  {saved}  {p.energy_rate_per_kwh}/kWh  PUE {p.pue}  "
                f"fees {p.fixed_fees_per_month}  {p.fee_spread.value}"
            )
    return 0


def _prices(args: argparse.Namespace) -> int:
    import getpass
    from decimal import Decimal, InvalidOperation

    from ai_meterix.core.config import load_config
    from ai_meterix.core.errors import AiMeterixError
    from ai_meterix.core.model import PriceSource
    from ai_meterix.pricing.engine import local_zone
    from ai_meterix.pricing.prices import (
        apply_reference,
        current_prices,
        load_reference,
        set_price,
    )
    from ai_meterix.storage import open_reader, open_writer

    from .prices import render_applied, render_history, render_prices, render_reference

    reference = load_reference()
    if args.action == "reference":
        print(render_reference(reference))
        return 0
    tz = local_zone()
    now = datetime.now(timezone.utc)
    writes = args.action == "set" or (args.action == "apply-reference" and not args.dry_run)
    if args.action == "set" and not args.model:
        print(
            "aimeterix prices: name the model, e.g. `prices set gpt-6-luna --input 0.10`",
            file=sys.stderr,
        )
        return 2
    url = args.url or load_config(args.config).storage.url
    try:
        storage = open_writer(url) if writes else open_reader(url)
    except AiMeterixError as exc:
        print(f"aimeterix prices: {exc}", file=sys.stderr)
        return 1
    try:
        currencies = set()
        for inventory in storage.get_hosts():
            profile = storage.get_profile(inventory.host.id)
            if profile is not None:
                currencies.add(profile.currency)
        currency = next(iter(currencies)) if len(currencies) == 1 else None
        if args.action == "history":
            print(render_history(storage.get_price_changes(args.model), tz))
        elif args.action == "set":
            try:
                values = {
                    name: Decimal(value)
                    for name, value in (
                        ("input_per_m", args.input),
                        ("output_per_m", args.output),
                        ("cache_read_per_m", args.cache_read),
                        ("cache_write_per_m", args.cache_write),
                    )
                    if value is not None
                }
            except InvalidOperation:
                print("aimeterix prices: prices are numbers per 1M tokens", file=sys.stderr)
                return 2
            start = (
                datetime.combine(args.since, time.min, tz).astimezone(timezone.utc)
                if args.since
                else None
            )
            source = PriceSource.LOCAL if args.local else None
            set_price(
                storage,  # type: ignore[arg-type]
                args.model,
                values,
                now=now,
                by=getpass.getuser(),
                start=start,
                source=source,
            )
            print(render_prices(current_prices(storage, now), currency, tz, reference))
        elif args.action == "apply-reference":
            mismatch = currencies - {reference.currency}
            if mismatch and not args.ignore_currency:
                print(
                    f"aimeterix prices: your cost profile is in {', '.join(sorted(mismatch))} "
                    f"but the reference list is in {reference.currency}, and AI Meterix does "
                    "not convert currencies (record 0012). Set prices yourself with "
                    "`aimeterix prices set`, or use the "
                    f"{reference.currency} numbers as they are with --ignore-currency.",
                    file=sys.stderr,
                )
                return 2
            results = apply_reference(
                storage,  # type: ignore[arg-type]
                reference,
                now=now,
                by=getpass.getuser(),
                dry_run=args.dry_run,
            )
            print(render_applied(results, args.dry_run))
        else:
            print(render_prices(current_prices(storage, now), currency, tz, reference))
    except AiMeterixError as exc:
        print(f"aimeterix prices: {exc}", file=sys.stderr)
        return 2
    finally:
        storage.close()
    return 0


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

    proxy = commands.add_parser("proxy", help="forward LLM calls and record their token counts")
    proxy.add_argument("--port", type=int, help="port on 127.0.0.1 (default 8787)")
    proxy.add_argument(
        "--upstream", help="where paths without a route name go, e.g. http://127.0.0.1:8080"
    )
    proxy.add_argument(
        "--route",
        action="append",
        type=_route,
        metavar="NAME=URL",
        help="add a route: /NAME/... goes to URL (repeatable)",
    )
    proxy.add_argument("--for", dest="duration", type=duration, help="stop after this long")
    proxy.add_argument("--config", help="path to aimeterix.yaml")
    proxy.add_argument(
        "--hash-key",
        action="store_true",
        help="read an API key on standard input and print its hash for the allowlist",
    )
    proxy.add_argument("-v", "--verbose", action="store_true", help="log every call's counts")
    proxy.set_defaults(func=_proxy)

    usage = commands.add_parser(
        "usage", help="token counts, energy and cost per model and day (record 0012)"
    )
    usage.add_argument("--from", dest="since", type=_day, help="first local day (default today)")
    usage.add_argument("--to", dest="until", type=_day, help="last local day, included")
    usage.add_argument("--last", type=duration, help="instead of days: the last 24h, 90m, ...")
    usage.add_argument(
        "--by",
        choices=("day", "model"),
        default="day",
        help="one row per model and day, or per model",
    )
    usage.add_argument("--csv", metavar="FILE", help="write CSV to FILE ('-' for the screen)")
    usage.add_argument("--url", help="storage URL (default: from the config)")
    usage.add_argument("--config", help="path to aimeterix.yaml")
    usage.set_defaults(func=_usage)

    profile = commands.add_parser("profile", help="show or set this machine's cost profile")
    profile.add_argument("action", nargs="?", choices=("show", "set", "history"), default="show")
    profile.add_argument("--rate", help="energy price per kWh in USD, e.g. 0.33")
    profile.add_argument("--pue", help="cooling and building overhead, 1.0 at home")
    profile.add_argument("--fees", help="fixed running fees per month in USD, e.g. 60")
    profile.add_argument("--spread", choices=("calendar", "uptime"), help="how fees are spread")
    profile.add_argument("--name", help="profile name, e.g. home-office")
    profile.add_argument("--region", help="label only, e.g. CZ")
    profile.add_argument("--host", help="machine id, when there is more than one")
    profile.add_argument("--url", help="storage URL (default: from the config)")
    profile.add_argument("--config", help="path to aimeterix.yaml")
    profile.set_defaults(func=_profile)

    prices = commands.add_parser("prices", help="show or set model prices per 1M tokens")
    prices.add_argument(
        "action",
        nargs="?",
        choices=("list", "set", "history", "reference", "apply-reference"),
        default="list",
    )
    prices.add_argument("model", nargs="?", help="model name, for set and history")
    prices.add_argument("--input", help="price per 1M input tokens")
    prices.add_argument("--output", help="price per 1M output tokens")
    prices.add_argument("--cache-read", help="price per 1M cache read tokens")
    prices.add_argument("--cache-write", help="price per 1M cache write tokens")
    prices.add_argument(
        "--from",
        dest="since",
        type=_day,
        help="start a new price period on this local day; earlier days keep their price",
    )
    prices.add_argument("--local", action="store_true", help="the model runs on this machine")
    prices.add_argument("--dry-run", action="store_true", help="apply-reference: only show")
    prices.add_argument(
        "--ignore-currency",
        action="store_true",
        help="apply-reference: use the USD numbers for a profile saved in another currency",
    )
    prices.add_argument("--url", help="storage URL (default: from the config)")
    prices.add_argument("--config", help="path to aimeterix.yaml")
    prices.set_defaults(func=_prices)
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
