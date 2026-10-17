"""The ``aimeterix`` command line."""

from __future__ import annotations

import argparse
import json
import logging
import re
import signal
import sys
import threading
from collections.abc import Callable, Sequence
from datetime import date, datetime, time, timezone
from typing import TYPE_CHECKING

from ai_meterix import __version__

if TYPE_CHECKING:
    from pathlib import Path

    from ai_meterix.core.config import Config
    from ai_meterix.core.model import Provider
    from ai_meterix.core.settings import SettingsWatcher
    from ai_meterix.storage import StorageWriter


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


def _opener(config: Config) -> Callable[[], StorageWriter]:
    from ai_meterix.storage import open_writer

    url = config.storage.url

    def open_storage() -> StorageWriter:
        if url.startswith("sqlite"):
            return open_writer(url, busy_timeout_ms=config.storage.busy_timeout_ms)
        return open_writer(url)

    return open_storage


def _live(config: Config) -> tuple[Config, tuple[int, ...] | None]:
    """``config`` with the live settings from the database (record 0013), seeding them
    on first use. When the database cannot be read the file's values are used and
    the settings watcher picks the database values up later."""
    from ai_meterix.core.settings import effective, seed_settings, settings_version

    logger = logging.getLogger("ai_meterix.settings")
    try:
        storage = _opener(config)()
        try:
            storage.migrate()
            added = seed_settings(storage, config)
            live, notes = effective(config, storage)
            version = settings_version(storage)
        finally:
            storage.close()
    except Exception as exc:
        logger.warning("could not read the settings (%s); using the config file for now", exc)
        return config, None
    if added:
        logger.info(
            "saved %d settings in the database; change them with `aimeterix config`", len(added)
        )
    for note in notes:
        logger.warning(note)
    return live, version


def _read_providers(config: Config) -> list[Provider] | None:
    """The providers in the database, or None when it cannot be read."""
    try:
        storage = _opener(config)()
        try:
            return storage.get_providers()
        finally:
            storage.close()
    except Exception as exc:
        logging.getLogger("ai_meterix.settings").warning("could not read the providers: %s", exc)
        return None


def _watch(
    config: Config,
    version: tuple[int, ...] | None,
    apply: Callable[[Config, list[str]], None],
    pinned: Sequence[str] = (),
    providers: list[Provider] | None = None,
) -> SettingsWatcher:
    from ai_meterix.core.settings import SettingsWatcher
    from ai_meterix.storage import open_reader

    watcher = SettingsWatcher(
        lambda: open_reader(config.storage.url), config, apply, version=version, pinned=pinned
    )
    watcher.providers = providers
    watcher.start()
    return watcher


def _agent(args: argparse.Namespace) -> int:
    from ai_meterix.collectors.agent import Agent, open_backend
    from ai_meterix.core.config import load_config

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config, version = _live(load_config(args.config))
    interval = args.interval or config.agent.interval_s
    try:
        backend = open_backend(args.project, mock=args.mock, interval_s=interval)
    except RuntimeError as exc:
        print(f"aimeterix agent: {exc}", file=sys.stderr)
        return 1
    try:
        agent = Agent(backend, _opener(config), interval_s=interval, burst_s=args.burst or 0.0)
    except ValueError as exc:
        print(f"aimeterix agent: {exc}", file=sys.stderr)
        return 2

    def apply(new: Config, changed: list[str]) -> None:
        if "agent.interval_s" in changed:
            agent.set_interval(new.agent.interval_s)

    pinned = ["agent.interval_s"] if args.interval else []
    watcher = _watch(config, version, apply, pinned)
    previous = {sig: signal.signal(sig, lambda *_: agent.stop()) for sig in _STOP_SIGNALS}
    burst = f", 1 s burst for {args.burst:.0f} s" if args.burst else ""
    logging.getLogger("ai_meterix.agent").info(
        "agent started: %s backend, every %.0f s%s; Ctrl-C to stop", backend.name, interval, burst
    )
    try:
        agent.run(args.duration)
    finally:
        watcher.stop()
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
    from ai_meterix.pricing.providers import local_names, routes

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
        config, version = _live(load_config(args.config))
        providers = _read_providers(config)
        settings = config.proxy.model_dump()
        settings["upstreams"] = {
            **(routes(providers) if providers is not None else settings["upstreams"]),
            **dict(args.route or []),
        }
        if args.upstream:
            settings["default_upstream"] = args.upstream
        if args.port is not None:  # 0 means any free port
            settings["port"] = args.port
        proxy_config = ProxyConfig.model_validate(settings)
    except (ConfigError, ValueError) as exc:
        print(f"aimeterix proxy: {exc}", file=sys.stderr)
        return 2
    router = Router(proxy_config.upstreams, proxy_config.default_upstream)
    recorder = Recorder(_opener(config))
    recorder.local_providers = {"local"} | local_names(providers or [])
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

    def apply(new: Config, changed: list[str]) -> None:
        if {"providers", "proxy.default_upstream"} & set(changed):
            try:
                live = ProxyConfig.model_validate(
                    {
                        **new.proxy.model_dump(),
                        "upstreams": {
                            **routes(watcher.providers or []),
                            **dict(args.route or []),
                        },
                        "default_upstream": args.upstream or new.proxy.default_upstream,
                    }
                )
            except ValueError as exc:
                logger.warning("routes not changed: %s", exc)
                return
            router.update(live.upstreams, live.default_upstream)
            recorder.local_providers = {"local"} | local_names(watcher.providers or [])
            for line in router.describe():
                logger.info("  %s%s", base, line)
        if "proxy.allowed_key_hashes" in changed:
            server.allowed = frozenset(new.proxy.allowed_key_hashes)

    pinned = ["proxy.default_upstream"] if args.upstream else []
    watcher = _watch(config, version, apply, pinned, providers)
    stop = threading.Event()
    previous = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in _STOP_SIGNALS}
    recorder.start()
    thread = threading.Thread(target=server.serve_forever, name="aimeterix-proxy", daemon=True)
    thread.start()
    try:
        stop.wait(args.duration)
    finally:
        watcher.stop()
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

    from .usage import period, render_csv, render_table

    tz = local_zone()
    now = datetime.now(timezone.utc)
    try:
        start, end = period(now, tz, args.since, args.until, args.last)
    except ValueError as exc:
        print(f"aimeterix usage: {exc}", file=sys.stderr)
        return 2
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

    from ai_meterix.core.config import load_config
    from ai_meterix.core.errors import AiMeterixError
    from ai_meterix.pricing.profiles import set_profile
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
            changes = {
                "rate": args.rate,
                "pue": args.pue,
                "fees": args.fees,
                "spread": args.spread,
                "name": args.name,
                "region": args.region,
            }
            try:
                current = set_profile(
                    storage,  # type: ignore[arg-type]
                    host,
                    changes,
                    by=getpass.getuser(),
                )
            except ValueError as exc:
                print(f"aimeterix profile: {exc}", file=sys.stderr)
                return 2
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
            known = {p.name for p in storage.get_providers()} | {"local"}
            if args.provider and args.provider not in known:
                print(
                    f"aimeterix prices: no provider {args.provider!r}; providers: "
                    f"{', '.join(sorted(known))} (add one with `aimeterix providers add`)",
                    file=sys.stderr,
                )
                return 2
            set_price(
                storage,  # type: ignore[arg-type]
                args.model,
                values,
                now=now,
                by=getpass.getuser(),
                start=start,
                source=source,
                provider=args.provider,
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
            rows = current_prices(storage, now)
            if args.provider:
                rows = [p for p in rows if p.provider == args.provider]
            print(render_prices(rows, currency, tz, reference))
    except AiMeterixError as exc:
        print(f"aimeterix prices: {exc}", file=sys.stderr)
        return 2
    finally:
        storage.close()
    return 0


def _serve(args: argparse.Namespace) -> int:
    from ai_meterix.core.config import load_config
    from ai_meterix.core.errors import AiMeterixError
    from ai_meterix.core.settings import seed_settings
    from ai_meterix.pricing.engine import local_zone
    from ai_meterix.storage import open_writer

    from .api import serve
    from .auth import defaults_unchanged, ensure_accounts

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config = load_config(args.config)
    url = args.url or config.storage.url
    host = args.host or config.api.host
    port = config.api.port if args.port is None else args.port  # 0: any free port
    try:
        storage = open_writer(url)
        try:
            storage.migrate()
            created = ensure_accounts(storage)
            unchanged = defaults_unchanged(storage)
            seed_settings(storage, config)
        finally:
            storage.close()
        server = serve(url, local_zone(), host=host, port=port, config=config)
    except (AiMeterixError, ValueError, OSError) as exc:
        print(f"aimeterix serve: {exc}", file=sys.stderr)
        return 1
    logger = logging.getLogger("ai_meterix.api")
    if created:
        logger.info("created accounts %s; each password is the account name", ", ".join(created))
    if unchanged:
        logger.info(
            "%s must change the first password: log in, then POST /api/password "
            "(or run `aimeterix password NAME`)",
            " and ".join(unchanged),
        )
    logger.info("API on http://%s:%d/api; Ctrl-C to stop", host, server.server_address[1])
    stop = threading.Event()
    previous = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in _STOP_SIGNALS}
    thread = threading.Thread(target=server.serve_forever, name="aimeterix-api", daemon=True)
    thread.start()
    try:
        stop.wait(args.duration)
    finally:
        server.shutdown()
        server.server_close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


def _password(args: argparse.Namespace) -> int:
    import getpass

    from ai_meterix.core.config import load_config
    from ai_meterix.core.errors import AiMeterixError
    from ai_meterix.storage import open_writer

    from .auth import ensure_accounts, set_password

    url = args.url or load_config(args.config).storage.url
    first = getpass.getpass(f"new password for {args.name}: ")
    if getpass.getpass("again: ") != first:
        print("aimeterix password: the two passwords differ", file=sys.stderr)
        return 2
    try:
        storage = open_writer(url)
    except AiMeterixError as exc:
        print(f"aimeterix password: {exc}", file=sys.stderr)
        return 1
    try:
        storage.migrate()
        ensure_accounts(storage)
        set_password(storage, args.name, first)
    except (AiMeterixError, ValueError) as exc:
        print(f"aimeterix password: {exc}", file=sys.stderr)
        return 2
    finally:
        storage.close()
    print(f"password changed for {args.name}")
    return 0


def _providers(args: argparse.Namespace) -> int:
    import getpass

    from ai_meterix.core.config import load_config
    from ai_meterix.core.errors import AiMeterixError
    from ai_meterix.core.settings import seed_settings
    from ai_meterix.pricing.engine import local_zone
    from ai_meterix.pricing.prices import current_prices
    from ai_meterix.pricing.providers import add_provider, set_provider
    from ai_meterix.storage import open_writer

    from .providers import render_provider, render_providers

    actions_with_name = ("show", "add", "set", "enable", "disable", "history")
    if args.action in actions_with_name and not args.name:
        print(f"aimeterix providers: {args.action} needs a provider name", file=sys.stderr)
        return 2
    try:
        config = load_config(args.config)
        if args.url:
            config = config.model_copy(
                update={"storage": config.storage.model_copy(update={"url": args.url})}
            )
        storage = open_writer(config.storage.url)
    except AiMeterixError as exc:
        print(f"aimeterix providers: {exc}", file=sys.stderr)
        return 1
    proxy = f"http://{config.proxy.host}:{config.proxy.port}"
    values = {
        "base_url": args.base_url,
        "app_path": args.path,
        "kind": args.kind,
        "label": args.label,
        "api_format": args.format,
    }
    try:
        storage.migrate()
        seed_settings(storage, config)
        by = getpass.getuser()
        if args.action == "add":
            add_provider(storage, args.name, values, by=by)
        elif args.action == "set":
            if all(v is None for v in values.values()):
                print("aimeterix providers: set needs at least one option", file=sys.stderr)
                return 2
            set_provider(storage, args.name, values, by=by)
        elif args.action in ("enable", "disable"):
            set_provider(storage, args.name, {"enabled": args.action == "enable"}, by=by)
        providers = storage.get_providers()
        now = datetime.now(timezone.utc)
        prices = current_prices(storage, now)
        if args.action == "list":
            print(render_providers(providers, prices, proxy))
            return 0
        found = {p.name: p for p in providers}.get(args.name)
        if found is None:
            names = ", ".join(sorted(found_name for found_name in {p.name for p in providers}))
            print(f"aimeterix providers: no provider {args.name!r}; providers: {names}",
                  file=sys.stderr)  # fmt: skip
            return 2
        if args.action == "history":
            changes = [
                c
                for c in storage.get_setting_changes()
                if c.key.startswith(f"provider.{args.name}.")
            ]
            tz = local_zone()
            for c in changes:
                field = c.key.rsplit(".", 1)[1]
                when = c.ts.astimezone(tz).strftime("%Y-%m-%d %H:%M")
                print(f"{when}  {c.by:<10} {field}: {c.old} -> {c.new}")
            if not changes:
                print("no changes yet")
            return 0
        print(render_provider(found, prices, proxy))
        if args.action in ("add", "set", "enable", "disable"):
            print("\nthe proxy picks this up within a minute")
        return 0
    except (AiMeterixError, ValueError) as exc:
        print(f"aimeterix providers: {exc}", file=sys.stderr)
        return 2
    finally:
        storage.close()


def _confirm(question: str) -> bool:
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:  # no terminal: never assume yes
        return False


def _init(args: argparse.Namespace) -> int:
    from ai_meterix.core.config import config_file, load_config
    from ai_meterix.core.errors import AiMeterixError
    from ai_meterix.core.settings import seed_settings
    from ai_meterix.storage import open_writer

    from .auth import ensure_accounts
    from .setup import legacy_home, safe_url, summary, write_starter

    path = config_file(args.config)
    old = legacy_home()
    if old is not None:
        print(
            f"Note: {old} is from a build before 0.1.0 and is no longer used. "
            "Delete it when you no longer need its data."
        )
    if not path.is_file():
        try:
            write_starter(path)
        except OSError as exc:
            print(f"aimeterix init: cannot write {path}: {exc}", file=sys.stderr)
            return 1
        print(f"Wrote the config file {path} with the default settings.")
        print()
    try:
        config = load_config(path)
    except AiMeterixError as exc:
        print(f"aimeterix init: {exc}", file=sys.stderr)
        return 2
    print("AI Meterix will set up:")
    for line in summary(config, path.resolve()):
        print(f"  {line}")
    print()
    if not args.yes and _confirm("Change any of these first?"):
        changed = _edit_config(path, config)
        if changed is None:
            return 2
        config = changed
        print()
        print("AI Meterix will set up:")
        for line in summary(config, path.resolve()):
            print(f"  {line}")
        print()
    if not args.yes and not _confirm("Create the database?"):
        print("Nothing changed.")
        return 1
    try:
        storage = open_writer(config.storage.url)
    except AiMeterixError as exc:
        print(f"aimeterix init: {exc}", file=sys.stderr)
        return 1
    try:
        version = storage.schema_version()
        if version is not None:
            print(
                f"aimeterix init: {safe_url(config.storage.url)} is already set up "
                f"(schema v{version}). Change settings with `aimeterix config` and "
                "`aimeterix providers`; to start again from nothing, run "
                "`aimeterix uninstall --purge` first.",
                file=sys.stderr,
            )
            return 1
        storage.migrate()
        ensure_accounts(storage)
        seed_settings(storage, config)
    finally:
        storage.close()
    print("Database created, with the accounts user and admin and the built-in providers.")
    print()
    try:
        from ai_meterix.collectors.probe import probe

        from .doctor import render

        print(render(probe()))
    except Exception as exc:  # the database is ready either way
        print(f"(could not check this machine: {exc}; run `aimeterix doctor`)")
    proxy, api = config.proxy, config.api
    print()
    print("Next:")
    print("  1. aimeterix start        run the agent, proxy and dashboard in the background")
    print("  2. aimeterix health       check everything is measuring")
    print(f"  3. open http://{api.host}:{api.port}/ and log in as user (password: user);")
    print(f"     the manual is at http://{api.host}:{api.port}/#help")
    print("  4. aimeterix profile set --rate 0.30   your electricity price per kWh (after start)")
    print(f"  5. point your apps at the proxy, e.g. http://{proxy.host}:{proxy.port}/openai/v1;")
    print("     `aimeterix providers` lists the address for each provider")
    return 0


def _ask(question: str, current: str) -> str:
    try:
        answer = input(f"  {question} [{current or 'none'}]: ").strip()
    except EOFError:
        return current
    return answer or current


def _edit_config(path: Path, config: Config) -> Config | None:
    """Ask for each main value (Enter keeps it), then write the file and check it."""
    from ai_meterix.core.config import load_config
    from ai_meterix.core.errors import AiMeterixError

    from .setup import CHOICES, set_value

    print("Press Enter to keep a value.")
    before = path.read_text(encoding="utf-8")
    text = before
    for choice in CHOICES:
        current = choice.current(config)
        answer = _ask(choice.question, current)
        if answer != current:
            text = set_value(text, choice.section, choice.key, answer)
    if text == before:
        return config
    path.write_text(text, encoding="utf-8")
    try:
        new = load_config(path)
    except AiMeterixError as exc:
        path.write_text(before, encoding="utf-8")
        print(f"aimeterix init: {exc}; the file was left as it was.", file=sys.stderr)
        return None
    print(f"Saved in {path}.")
    return new


def _uninstall(args: argparse.Namespace) -> int:
    from ai_meterix.core.config import load_config
    from ai_meterix.core.errors import AiMeterixError
    from ai_meterix.core.paths import paths

    from .service import SERVICES, stop
    from .setup import legacy_home, purge, removal_hint, sqlite_path

    ok = True
    for name, stopped, message in stop(SERVICES):
        print(f"{name:<6} {message}")
        ok = ok and stopped
    if not ok:
        print("aimeterix uninstall: something is still running; nothing deleted", file=sys.stderr)
        return 1
    folders = paths().folders()
    outside = None
    try:
        db = sqlite_path(load_config(args.config).storage.url)
        if db is not None and not any(db.resolve().is_relative_to(f.resolve()) for f in folders):
            outside = db
    except AiMeterixError:
        pass
    if args.purge:
        print("This deletes for good the config, database, logs and buffers in:")
        for folder in folders:
            print(f"  {folder}")
        if not args.yes and not _confirm("Delete them?"):
            print("Nothing deleted.")
            return 1
        try:
            deleted = purge()
        except (OSError, ValueError) as exc:
            print(f"aimeterix uninstall: {exc}", file=sys.stderr)
            return 1
        print("\n".join(f"Deleted {f}." for f in deleted) or "Nothing was there to delete.")
    else:
        print("Your config and data are kept; `aimeterix uninstall --purge` deletes them.")
        print("`aimeterix paths` shows where they are.")
    if outside is not None:
        print(f"Your database {outside} is outside these folders and was not deleted.")
    old = legacy_home()
    if old is not None:
        print(f"{old} is from a build before 0.1.0; delete it yourself if you no longer need it.")
    print(f"To remove the program: {removal_hint()}")
    return 0


def _paths(args: argparse.Namespace) -> int:
    from ai_meterix.core.config import config_file
    from ai_meterix.core.paths import paths

    from .setup import legacy_home, where_lines

    for line in where_lines(paths(), config_file(args.config)):
        print(line)
    old = legacy_home()
    if old is not None:
        print(f"Old folder     {old}  (from a build before 0.1.0, no longer used)")
    return 0


def _config(args: argparse.Namespace) -> int:
    import getpass

    from ai_meterix.core.config import load_config
    from ai_meterix.core.errors import AiMeterixError
    from ai_meterix.core.settings import (
        LIVE,
        effective,
        export_yaml,
        live_values,
        parse_value,
        reset_setting,
        seed_settings,
        set_setting,
    )
    from ai_meterix.storage import open_writer

    try:
        config = load_config(args.config)
        if args.url:
            config = config.model_copy(
                update={"storage": config.storage.model_copy(update={"url": args.url})}
            )
        storage = open_writer(config.storage.url)
    except AiMeterixError as exc:
        print(f"aimeterix config: {exc}", file=sys.stderr)
        return 1
    try:
        storage.migrate()
        seed_settings(storage, config)
        if args.action in ("set", "reset"):
            if not args.key or (args.action == "set" and args.value is None):
                print(
                    f"aimeterix config: {args.action} needs a setting name"
                    + (" and a value" if args.action == "set" else ""),
                    file=sys.stderr,
                )
                return 2
            if args.action == "set":
                value = set_setting(
                    storage,
                    config,
                    args.key,
                    parse_value(args.key, args.value),
                    by=getpass.getuser(),
                )
            else:
                value = reset_setting(storage, config, args.key, by=getpass.getuser())
            print(f"{args.key} = {json.dumps(value)}")
            print("the agent and proxy pick this up within a minute")
            return 0
        if args.action == "history":
            if args.key and args.key not in LIVE:
                print(f"aimeterix config: unknown setting {args.key!r}", file=sys.stderr)
                return 2
            changes = [c for c in storage.get_setting_changes(args.key) if c.key in LIVE]
            for c in changes:
                print(f"{c.ts:%Y-%m-%d %H:%M} UTC  {c.by:<6}  {c.key}  {c.old} -> {c.new}")
            if not changes:
                print("no changes yet")
            return 0
        live, notes = effective(config, storage)
        if args.action == "export":
            text = export_yaml(live)
            if args.key and args.key != "-":
                with open(args.key, "x", encoding="utf-8") as out:
                    out.write(text)
                print(f"wrote {args.key}")
            else:
                print(text, end="")
            return 0
        saved = storage.get_settings()
        print("Live settings (in the database; change with `aimeterix config set KEY VALUE`)")
        for key, value in live_values(live).items():
            print(f"  {key:<30} {json.dumps(value)}  (version {saved[key].version})")
        print("Bootstrap settings (config file or environment; need a restart)")
        print(f"  {'storage.url':<30} {live.storage.url}")
        print(f"  {'proxy':<30} {live.proxy.host}:{live.proxy.port}")
        print(f"  {'api':<30} {live.api.host}:{live.api.port}")
        for note in notes:
            print(f"note: {note}")
        return 0
    except FileExistsError:
        print(f"aimeterix config: {args.key} exists already", file=sys.stderr)
        return 2
    except (AiMeterixError, ValueError) as exc:
        print(f"aimeterix config: {exc}", file=sys.stderr)
        return 2
    finally:
        storage.close()


def _start(args: argparse.Namespace) -> int:
    from ai_meterix.core.config import config_file, load_config
    from ai_meterix.core.errors import ConfigError

    from .service import SERVICES, start
    from .setup import write_starter

    # The first start sets itself up with the defaults; `init` is only for
    # choosing settings before that. The processes create the database.
    path = config_file(args.config)
    first = not path.is_file()
    if first:
        try:
            write_starter(path)
        except OSError as exc:
            print(f"aimeterix start: cannot write {path}: {exc}", file=sys.stderr)
            return 1
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"aimeterix start: {exc}", file=sys.stderr)
        return 2
    names = args.names or list(SERVICES)
    ok = True
    for name, started, message in start(names, config=args.config):
        print(f"{name:<6} {message}")
        ok = ok and started
    api, proxy = config.api, config.proxy
    print(f"Dashboard: http://{api.host}:{api.port}/  (log in as user, password user)")
    print(f"Manual:    http://{api.host}:{api.port}/#help")
    print(f"Proxy for your apps: http://{proxy.host}:{proxy.port}  (`aimeterix providers`)")
    if first:
        print(f"First start: the default settings are in {path}.")
        print("Change ports or the database there, then `aimeterix stop` and `aimeterix start`;")
        print("everything else with `aimeterix config`.")
    print("Check with `aimeterix health`; stop with `aimeterix stop`.")
    return 0 if ok else 1


def _stop(args: argparse.Namespace) -> int:
    from ai_meterix.core.config import load_config
    from ai_meterix.core.errors import ConfigError

    from .service import SERVICES, port_in_use, stop

    names = args.names or [n for n in SERVICES if not (args.keep_proxy and n == "proxy")]
    ok = True
    for name, stopped, message in stop(names):
        print(f"{name:<6} {message}")
        ok = ok and stopped
    try:
        config = load_config(args.config)
    except ConfigError:
        return 0 if ok else 1
    ports = {
        "proxy": (config.proxy.host, config.proxy.port),
        "serve": (config.api.host, config.api.port),
    }
    for name in names:
        if name in ports and port_in_use(*ports[name]):
            print(f"{name:<6} port {ports[name][1]} is still in use by another program")
            ok = False
    return 0 if ok else 1


def _health(args: argparse.Namespace) -> int:
    from ai_meterix.core.config import load_config
    from ai_meterix.core.errors import ConfigError

    from .service import health

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"aimeterix health: {exc}", file=sys.stderr)
        return 2
    report = health(args.url or config.storage.url)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0 if report["ok"] else 1
    now = datetime.now(timezone.utc)

    def age(ts: datetime | None) -> str:
        if ts is None:
            return "none in the last 6 h"
        seconds = int((now - ts).total_seconds())
        return f"{seconds // 60} min ago" if seconds >= 120 else "just now"

    for p in report["processes"]:
        if p["running"]:
            up = p["uptime_s"] or 0
            uptime = f"{up // 3600} h {up % 3600 // 60} min"
            print(f"  {p['name']:<6} running   pid {p['pid']}, up {uptime}")
        else:
            print(f"  {p['name']:<6} stopped")
    db = report["database"]
    size = f", {db['size_bytes'] / 1e6:.1f} MB" if db["size_bytes"] else ""
    print(f"  database {'ok' if db['ok'] else 'NOT OK'} ({db['backend'] or '-'}{size})")
    print(f"  last hardware sample  {age(report['last_sample'])}")
    print(f"  last proxied call     {age(report['last_call'])}")
    print("healthy" if report["ok"] else "NOT healthy: " + "; ".join(report["problems"]))
    return 0 if report["ok"] else 1


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
    prices.add_argument(
        "--provider", help="set: the provider the model belongs to; list: only this provider"
    )
    prices.add_argument("--dry-run", action="store_true", help="apply-reference: only show")
    prices.add_argument(
        "--ignore-currency",
        action="store_true",
        help="apply-reference: use the USD numbers for a profile saved in another currency",
    )
    prices.add_argument("--url", help="storage URL (default: from the config)")
    prices.add_argument("--config", help="path to aimeterix.yaml")
    prices.set_defaults(func=_prices)

    serve = commands.add_parser("serve", help="run the localhost API with login (record 0013)")
    serve.add_argument("--host", help="address to listen on (default 127.0.0.1)")
    serve.add_argument("--port", type=int, help="port (default 8788)")
    serve.add_argument("--for", dest="duration", type=duration, help="stop after this long")
    serve.add_argument("--url", help="storage URL (default: from the config)")
    serve.add_argument("--config", help="path to aimeterix.yaml")
    serve.add_argument("-v", "--verbose", action="store_true", help="log every request")
    serve.set_defaults(func=_serve)

    password = commands.add_parser("password", help="set the password of the user or admin account")
    password.add_argument("name", choices=("user", "admin"))
    password.add_argument("--url", help="storage URL (default: from the config)")
    password.add_argument("--config", help="path to aimeterix.yaml")
    password.set_defaults(func=_password)

    config = commands.add_parser(
        "config", help="show or change the live settings in the database (record 0013)"
    )
    config.add_argument(
        "action", nargs="?", choices=("show", "set", "reset", "history", "export"), default="show"
    )
    config.add_argument("key", nargs="?", help="setting name, e.g. agent.interval_s; export: FILE")
    config.add_argument("value", nargs="?", help="new value: a number, text, null or JSON")
    config.add_argument("--url", help="storage URL (default: from the config)")
    config.add_argument("--config", help="path to aimeterix.yaml")
    config.set_defaults(func=_config)

    providers = commands.add_parser(
        "providers", help="list, add or change the AI providers the proxy routes to"
    )
    providers.add_argument(
        "action",
        nargs="?",
        choices=("list", "show", "add", "set", "enable", "disable", "history"),
        default="list",
    )
    providers.add_argument("name", nargs="?", help="provider name, e.g. groq (the route name)")
    providers.add_argument("--base-url", help="the provider's address, e.g. https://api.groq.com")
    providers.add_argument("--path", help="path an app adds after it, e.g. /openai/v1")
    providers.add_argument(
        "--kind", choices=("cloud", "local"), help="local: runs on this machine (no token price)"
    )
    providers.add_argument("--label", help="display name, e.g. Groq")
    providers.add_argument(
        "--format",
        choices=("openai", "anthropic", "gemini", "ollama"),
        help="the API's response format (default openai)",
    )
    providers.add_argument("--url", help="storage URL (default: from the config)")
    providers.add_argument("--config", help="path to aimeterix.yaml")
    providers.set_defaults(func=_providers)

    init = commands.add_parser(
        "init", help="optional, before the first start: review and change the default settings"
    )
    init.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    init.add_argument("--config", help="path to aimeterix.yaml (default: in the config folder)")
    init.set_defaults(func=_init)

    uninstall = commands.add_parser(
        "uninstall", help="stop everything; --purge also deletes the data"
    )
    uninstall.add_argument(
        "--purge", action="store_true", help="also delete the config, data and logs folders"
    )
    uninstall.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    uninstall.add_argument("--config", help="path to aimeterix.yaml")
    uninstall.set_defaults(func=_uninstall)

    where = commands.add_parser("paths", help="show where the config, data and logs are")
    where.add_argument("--config", help="path to aimeterix.yaml")
    where.set_defaults(func=_paths)

    names = ("agent", "proxy", "serve")

    def service(text: str) -> str:
        # Not `choices`: before Python 3.12, argparse checks an empty `nargs="*"` list
        # against the choices and fails when no name is given.
        if text not in names:
            raise argparse.ArgumentTypeError(f"{text!r} is not one of {', '.join(names)}")
        return text

    start = commands.add_parser("start", help="run the agent, proxy and API in the background")
    start.add_argument(
        "names",
        nargs="*",
        type=service,
        metavar="{agent,proxy,serve}",
        help="only these (default all)",
    )
    start.add_argument("--config", help="path to aimeterix.yaml")
    start.set_defaults(func=_start)

    stop = commands.add_parser("stop", help="stop what `start` runs; buffers are written first")
    stop.add_argument(
        "names",
        nargs="*",
        type=service,
        metavar="{agent,proxy,serve}",
        help="only these (default all)",
    )
    stop.add_argument(
        "--keep-proxy", action="store_true", help="leave the proxy running for your apps"
    )
    stop.add_argument("--config", help="path to aimeterix.yaml")
    stop.set_defaults(func=_stop)

    health = commands.add_parser("health", help="is everything running and measuring now?")
    health.add_argument("--json", action="store_true", help="print the report as JSON")
    health.add_argument("--url", help="storage URL (default: from the config)")
    health.add_argument("--config", help="path to aimeterix.yaml")
    health.set_defaults(func=_health)
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
