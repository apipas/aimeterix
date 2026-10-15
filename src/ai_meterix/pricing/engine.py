"""The cost engine (PLAN.md Phase 3 step 4, record 0012).

Costs are derived, never stored: every statement is computed on demand from
what storage holds, so editing a price or a cost profile changes past figures
at once. All money uses ``Decimal``.

For each hour and host:

- **energy**: chip energy measured that hour (kWh) x PUE x the energy rate;
- **fees**: the profile's fixed fees per month / the hours of that month
  (``calendar``: the real hours of the local month, 672 to 744 with daylight
  saving; ``uptime``: the hours the agent saw the machine that month);
- the hour's energy and fees are split by **energy share**: each local model
  gets the share its processes used, other apps share one "other apps" line,
  and what nobody used (an idle chip, or an hour the machine was off but the
  fee still ran) is the "idle overhead" line;
- **tokens** x the model's price per 1M tokens of each type, at the time of
  the call. An API model with no price, or ``price_set = False``, costs 0 and
  is marked "price not set". A dated snapshot with no row of its own
  (``claude-opus-4-1-20250805``) uses its base model's price.

The profile in force at an hour is the last version saved by then; hours
before the first version use the first one. Hours come from raw samples where
they still exist, else from the hourly rollups. Days are local days.

Per-app energy is an estimate (record 0011), so a local model's cost is too.
"""

from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ai_meterix.core.model import CURRENCY, CostProfile, FeeSpread, ModelPrice
from ai_meterix.core.timeutil import ensure_utc
from ai_meterix.pricing.prices import base_model, price_at
from ai_meterix.storage.base import Dataset, StorageReader, UsageQuery

HOUR = timedelta(hours=1)
J_PER_KWH = Decimal(3_600_000)
MILLION = Decimal(1_000_000)
OTHER_APPS = "(other apps)"
IDLE = "(idle overhead)"
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
_PRICE_FIELDS = dict(
    zip(
        TOKEN_FIELDS,
        ("input_per_m", "output_per_m", "cache_read_per_m", "cache_write_per_m"),
        strict=True,
    )
)
PRICE_NOT_SET = "price not set"
NO_PROFILE = "no cost profile"
ESTIMATE = "energy share is an estimate"


def local_zone() -> tzinfo:
    """This machine's time zone, with its daylight-saving rules."""
    name = os.environ.get("TZ")
    if not name:
        try:
            target = os.path.realpath("/etc/localtime")
            name = target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else None
        except OSError:
            name = None
    try:
        return ZoneInfo(name) if name else timezone.utc
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def month_hours(hour: datetime, tz: tzinfo) -> Decimal:
    """Real hours in the local calendar month that contains ``hour``."""
    local = hour.astimezone(tz)
    start = datetime(local.year, local.month, 1, tzinfo=tz)
    nxt = datetime(local.year + local.month // 12, local.month % 12 + 1, 1, tzinfo=tz)
    seconds = (nxt.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds()
    return Decimal(int(seconds)) / 3600


def _dec(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


# --- what goes in --------------------------------------------------------------------


@dataclass
class _Hour:
    """Measured use of one host in one hour."""

    energy_j: float = 0.0
    seconds: float = 0.0  # how long the agent sampled (longest device)
    models: dict[str, float] = field(default_factory=dict)  # model name -> energy J
    other_j: float = 0.0  # attributed to apps that are not model runtimes


@dataclass
class _Tokens:
    requests: int = 0
    counts: dict[str, int | None] = field(default_factory=lambda: dict.fromkeys(TOKEN_FIELDS))
    cost: Decimal = Decimal(0)

    def add(self, row: Mapping[str, Any], price: ModelPrice | None) -> None:
        self.requests += int(row["requests"] or 0)
        for name in TOKEN_FIELDS:
            value = row.get(name)
            if value is None:
                continue
            self.counts[name] = (self.counts[name] or 0) + int(value)
            if price is not None:
                self.cost += Decimal(int(value)) * getattr(price, _PRICE_FIELDS[name]) / MILLION


# --- what comes out ------------------------------------------------------------------


@dataclass
class UsageLine:
    """One model (or the other-apps or idle line) on one local day."""

    day: date
    model: str
    source: str  # "local", "api" or "" for the other-apps and idle lines
    provider: str = ""
    requests: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    kwh: Decimal = Decimal(0)
    energy_cost: Decimal = Decimal(0)
    fee_cost: Decimal = Decimal(0)
    token_cost: Decimal = Decimal(0)
    notes: set[str] = field(default_factory=set)

    @property
    def total_tokens(self) -> int | None:
        counts = [getattr(self, name) for name in TOKEN_FIELDS]
        present = [c for c in counts if c is not None]
        return sum(present) if present else None

    @property
    def cost(self) -> Decimal:
        return self.energy_cost + self.fee_cost + self.token_cost

    def as_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "model": self.model,
            "source": self.source,
            "provider": self.provider,
            "requests": self.requests,
            **{name: getattr(self, name) for name in TOKEN_FIELDS},
            "total_tokens": self.total_tokens,
            "kwh": self.kwh,
            "energy_cost": self.energy_cost,
            "fee_cost": self.fee_cost,
            "token_cost": self.token_cost,
            "cost": self.cost,
            "notes": sorted(self.notes),
        }


@dataclass
class Statement:
    start: datetime
    end: datetime
    currency: str | None
    tz: str
    lines: list[UsageLine]

    def by_model(self) -> list[UsageLine]:
        """The same lines added up over all days (``day`` is the first day)."""
        merged: dict[tuple[str, str, str], UsageLine] = {}
        for line in self.lines:
            key = (line.model, line.source, line.provider)
            if key not in merged:
                merged[key] = UsageLine(line.day, line.model, line.source, line.provider)
            _merge(merged[key], line)
        return sorted(merged.values(), key=_order)

    def total(self) -> UsageLine:
        total = UsageLine(self.start.date(), "Total", "")
        for line in self.lines:
            _merge(total, line)
        return total


def _merge(into: UsageLine, line: UsageLine) -> None:
    into.requests += line.requests
    for name in TOKEN_FIELDS:
        value = getattr(line, name)
        if value is not None:
            setattr(into, name, (getattr(into, name) or 0) + value)
    into.kwh += line.kwh
    into.energy_cost += line.energy_cost
    into.fee_cost += line.fee_cost
    into.token_cost += line.token_cost
    into.notes |= line.notes


def _order(line: UsageLine) -> tuple[Any, ...]:
    special = {OTHER_APPS: 1, IDLE: 2}.get(line.model, 0)
    return (line.day, special, line.source != "api", line.model)


# --- the engine ----------------------------------------------------------------------


class CostEngine:
    def __init__(self, storage: StorageReader, tz: tzinfo | None = None) -> None:
        self.storage = storage
        self.tz = tz or local_zone()

    # reading -------------------------------------------------------------------------

    def _hourly(
        self, raw: Dataset, rolled: Dataset, start: datetime, end: datetime, group: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        """Rows per hour: raw where the hour still has raw rows, else the rollup."""
        rows = self.storage.query(UsageQuery(raw, start, end, bucket=HOUR, group_by=group)).dicts()
        have = {r["bucket"] for r in rows}
        old = self.storage.query(UsageQuery(rolled, start, end, bucket=HOUR, group_by=group))
        rows += [r for r in old.dicts() if r["bucket"] not in have]
        return rows

    def _hours(self, start: datetime, end: datetime) -> dict[str, dict[datetime, _Hour]]:
        models = {m.id: m.model_name for m in self.storage.get_local_models()}
        hosts: dict[str, dict[datetime, _Hour]] = defaultdict(lambda: defaultdict(_Hour))
        devices = self._hourly(
            Dataset.DEVICE, Dataset.DEVICE_HOURLY, start, end, ("host_id", "device_id")
        )
        for r in devices:
            hour = hosts[r["host_id"]][r["bucket"]]
            hour.energy_j += r["energy_j"] or 0.0
            hour.seconds = max(hour.seconds, r["seconds"] or 0.0)
        apps = self._hourly(
            Dataset.PROCESS, Dataset.PROCESS_HOURLY, start, end, ("host_id", "local_model_id")
        )
        for r in apps:
            energy = r["energy_j"] or 0.0
            hour = hosts[r["host_id"]][r["bucket"]]
            model_id = r["local_model_id"]
            if model_id:
                name = models.get(model_id, model_id)
                hour.models[name] = hour.models.get(name, 0.0) + energy
            else:
                hour.other_j += energy
        return hosts

    def _uptime_hours(self, hours: Mapping[datetime, _Hour], hour: datetime) -> Decimal:
        """Hours the agent saw the machine in the local month of ``hour``."""
        local = hour.astimezone(self.tz)
        key = (local.year, local.month)
        cache = self._uptime_cache
        if key not in cache:
            start = datetime(local.year, local.month, 1, tzinfo=self.tz)
            nxt = datetime(local.year + local.month // 12, local.month % 12 + 1, 1, tzinfo=self.tz)
            seen = self._hours(start.astimezone(timezone.utc), nxt.astimezone(timezone.utc))
            seconds = sum(h.seconds for host in seen.values() for h in host.values())
            cache[key] = _dec(seconds) / 3600
        return cache[key]

    def coverage(
        self, start: datetime, end: datetime, now: datetime | None = None
    ) -> list[tuple[date, float, float]]:
        """Per local day: hours the agent measured, and hours there were to measure.

        With several machines, the measured hours are their average.
        """
        start, end = ensure_utc(start), ensure_utc(end)
        last = min(end, ensure_utc(now) if now else datetime.now(timezone.utc))
        hosts = self._hours(start, end)
        measured: dict[date, float] = defaultdict(float)
        for hours in hosts.values():
            for ts, hour in hours.items():
                day = ts.astimezone(self.tz).date()
                measured[day] += min(hour.seconds, 3600.0) / 3600 / max(len(hosts), 1)
        out = []
        day = start.astimezone(self.tz).date()
        while True:
            day_start = datetime.combine(day, datetime.min.time(), self.tz)
            if day_start.astimezone(timezone.utc) >= last:
                break
            day_end = datetime.combine(day + timedelta(days=1), datetime.min.time(), self.tz)
            lo = max(day_start.astimezone(timezone.utc), start)
            hi = min(day_end.astimezone(timezone.utc), last)
            out.append((day, round(measured.get(day, 0.0), 2), (hi - lo).total_seconds() / 3600))
            day += timedelta(days=1)
        return out

    # computing -----------------------------------------------------------------------

    def statement(self, start: datetime, end: datetime, now: datetime | None = None) -> Statement:
        """Usage and cost per local day and model for ``[start, end)``."""
        start, end = ensure_utc(start), ensure_utc(end)
        now = ensure_utc(now) if now else datetime.now(timezone.utc)
        self._uptime_cache: dict[tuple[int, int], Decimal] = {}
        lines: dict[tuple[date, str, str, str], UsageLine] = {}
        currencies: set[str] = set()
        local_names = {m.model_name for m in self.storage.get_local_models()}

        def line(day: date, model: str, source: str, provider: str = "") -> UsageLine:
            key = (day, model, source, provider)
            if key not in lines:
                lines[key] = UsageLine(day, model, source, provider)
            return lines[key]

        # hardware: energy and fees, split by energy share
        hosts = self._hours(start, end)
        for host in self.storage.get_hosts():
            hosts.setdefault(host.host.id, defaultdict(_Hour))
        for host_id, hours in hosts.items():
            history = self.storage.get_profile_history(host_id)
            if history:
                currencies.update(p.currency for p in history)
            first = _floor_hour(start)
            last = min(end, now)
            ts = first
            while ts < last:
                measured = hours.get(ts)
                profile = _profile_at(history, ts)
                self._cost_hour(line, ts, measured or _Hour(), profile, hours, measured is not None)
                ts += HOUR

        # tokens: per call hour, priced at that hour
        prices = _price_index(self.storage.get_prices())
        calls = self._hourly(
            Dataset.CALLS, Dataset.CALLS_HOURLY, start, end, ("provider", "model", "call_type")
        )
        for r in calls:
            ts = r["bucket"]
            day = ts.astimezone(self.tz).date()
            local = r["provider"] == "local" or r["model"] in local_names
            source = "local" if local else "api"
            target = line(day, r["model"], source, "" if local else r["provider"])
            rows = prices.get(r["model"]) or prices.get(base_model(r["model"]), [])
            price = price_at(rows, ts)
            tokens = _Tokens()
            tokens.add(r, price if price is not None and price.price_set else None)
            target.requests += tokens.requests
            for name in TOKEN_FIELDS:
                value = tokens.counts[name]
                if value is not None:
                    setattr(target, name, (getattr(target, name) or 0) + value)
            target.token_cost += tokens.cost
            if r["call_type"] == "batch":
                target.notes.add("batch tokens not metered yet")
            if not local and (price is None or not price.price_set):
                target.notes.add(PRICE_NOT_SET)

        # money is in USD for now (record 0012); older versions may name another
        currency = CURRENCY if currencies else None
        ordered = sorted(lines.values(), key=_order)
        return Statement(start, end, currency, str(self.tz), ordered)

    def _cost_hour(
        self,
        line: Any,
        ts: datetime,
        hour: _Hour,
        profile: CostProfile | None,
        hours: Mapping[datetime, _Hour],
        measured: bool,
    ) -> None:
        day = ts.astimezone(self.tz).date()
        kwh = _dec(hour.energy_j) / J_PER_KWH
        if profile is None:
            energy_cost = fee = Decimal(0)
        else:
            energy_cost = kwh * profile.pue * profile.energy_rate_per_kwh
            fee = Decimal(0)
            if profile.fixed_fees_per_month:
                if profile.fee_spread is FeeSpread.CALENDAR:
                    fee = profile.fixed_fees_per_month / month_hours(ts, self.tz)
                elif measured and hour.seconds > 0:
                    on = self._uptime_hours(hours, ts)
                    if on > 0:
                        fee = profile.fixed_fees_per_month * (_dec(hour.seconds) / 3600) / on
        if not measured and fee == 0:
            return
        shares: dict[str, Decimal] = {}
        if hour.energy_j > 0:
            total = _dec(hour.energy_j)
            for name, joules in hour.models.items():
                shares[name] = min(_dec(joules) / total, Decimal(1))
            shares[OTHER_APPS] = min(_dec(hour.other_j) / total, Decimal(1))
            used = sum(shares.values(), Decimal(0))
            if used > 1:  # attributed more than measured: scale down
                shares = {k: v / used for k, v in shares.items()}
        idle = Decimal(1) - sum(shares.values(), Decimal(0))
        shares[IDLE] = max(idle, Decimal(0))
        for name, share in shares.items():
            if share == 0:
                continue
            source = "" if name in (OTHER_APPS, IDLE) else "local"
            target = line(day, name, source)
            target.kwh += kwh * share
            target.energy_cost += energy_cost * share
            target.fee_cost += fee * share
            if profile is None:
                target.notes.add(NO_PROFILE)
            elif source == "local":
                target.notes.add(ESTIMATE)


def _floor_hour(ts: datetime) -> datetime:
    return ts.replace(minute=0, second=0, microsecond=0)


def _profile_at(history: list[CostProfile], ts: datetime) -> CostProfile | None:
    if not history:
        return None
    chosen = history[0]  # before the first version: the first one
    for profile in history:
        if profile.saved_at is not None and profile.saved_at <= ts:
            chosen = profile
    return chosen


def _price_index(prices: Iterable[ModelPrice]) -> dict[str, list[ModelPrice]]:
    index: dict[str, list[ModelPrice]] = defaultdict(list)
    for price in prices:
        index[price.model].append(price)
    return index


__all__ = [
    "IDLE",
    "OTHER_APPS",
    "PRICE_NOT_SET",
    "CostEngine",
    "Statement",
    "UsageLine",
    "local_zone",
    "month_hours",
]
