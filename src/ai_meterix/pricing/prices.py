"""Model prices: editing, history and the reference list (record 0012 D3.7).

Prices live in the ``model_prices`` table, one row per model and validity
period, in the profile's currency per 1M tokens. Every edit is logged in
``price_changes`` by storage. Costs are derived, so an edit re-prices the
period it covers at once.

- ``set_price`` without a start date corrects the price in force now (its whole
  period is re-priced); with a start date it begins a new period there and
  ends the one before, so earlier days keep their price.
- ``ensure_price_row`` gives a model seen for the first time its default row:
  0 for a local model, 0 with ``price_set = False`` ("price not set") for an
  API model.
- ``apply_reference`` copies the shipped, dated list of public prices of the
  built-in cloud providers into the table. It runs only when an admin asks.
- Each row names the provider its model belongs to (record 0018). A model with
  no price, or one still at "price not set", costs 0.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from importlib import resources
from typing import Any

from ai_meterix.core.model import ModelPrice, PriceSource
from ai_meterix.storage.base import StorageReader, StorageWriter

PRICE_FIELDS = ("input_per_m", "output_per_m", "cache_read_per_m", "cache_write_per_m")
_REFERENCE_KEYS = dict(
    zip(("input", "output", "cache_read", "cache_write"), PRICE_FIELDS, strict=True)
)
# a dated snapshot of a model: claude-opus-4-1-20250805, o4-mini-2025-04-16
_SNAPSHOT = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2})$")


def base_model(model: str) -> str:
    """The model name without a trailing snapshot date."""
    return _SNAPSHOT.sub("", model)


@dataclass(frozen=True, slots=True)
class ReferencePrice:
    provider: str
    model: str
    input_per_m: Decimal
    output_per_m: Decimal
    cache_read_per_m: Decimal
    cache_write_per_m: Decimal


@dataclass(frozen=True, slots=True)
class ReferenceList:
    checked: str
    currency: str
    sources: Mapping[str, str]
    notes: tuple[str, ...]
    models: tuple[ReferencePrice, ...]

    def find(self, model: str) -> ReferencePrice | None:
        names = {r.model: r for r in self.models}
        return names.get(model) or names.get(base_model(model))


def load_reference(text: str | None = None) -> ReferenceList:
    """The shipped reference list, or one given as JSON text."""
    if text is None:
        text = (
            resources.files("ai_meterix.pricing")
            .joinpath("reference_prices.json")
            .read_text(encoding="utf-8")
        )
    data: dict[str, Any] = json.loads(text)
    models = tuple(
        ReferencePrice(
            str(m["provider"]),
            str(m["model"]),
            **{field: Decimal(str(m.get(key) or 0)) for key, field in _REFERENCE_KEYS.items()},
        )
        for m in data["models"]
    )
    return ReferenceList(
        str(data["checked"]),
        str(data["currency"]),
        dict(data.get("sources", {})),
        tuple(data.get("notes", ())),
        models,
    )


def price_at(prices: Iterable[ModelPrice], ts: datetime) -> ModelPrice | None:
    """The row in force at ``ts``; before the first row, the first row."""
    rows = list(prices)
    found = None
    for price in rows:
        if price.valid_from <= ts and (price.valid_to is None or ts < price.valid_to):
            found = price
    if found is None and rows and all(p.valid_from > ts for p in rows):
        found = min(rows, key=lambda p: p.valid_from)  # the first price has no start
    return found


def current_prices(storage: StorageReader, now: datetime) -> list[ModelPrice]:
    """One row per model: the price in force now."""
    by_model: dict[str, list[ModelPrice]] = {}
    for price in storage.get_prices():
        by_model.setdefault(price.model, []).append(price)
    found = (price_at(rows, now) for rows in by_model.values())
    return sorted((p for p in found if p is not None), key=lambda p: p.model)


def _values(price: ModelPrice) -> tuple[Decimal, ...]:
    return tuple(getattr(price, f) for f in PRICE_FIELDS)


def set_price(
    storage: StorageWriter,
    model: str,
    values: Mapping[str, Decimal],
    *,
    now: datetime,
    by: str,
    start: datetime | None = None,
    source: PriceSource | None = None,
    provider: str | None = None,
) -> ModelPrice:
    """Set ``model``'s prices; fields not in ``values`` keep their current value.

    Without ``start`` the row in force now is corrected. With ``start`` a new
    period begins there: the row it falls in ends at ``start``, and the new
    row runs to where that row ran. ``provider`` names the provider the model
    belongs to, on every period of it.
    """
    rows = sorted(storage.get_prices(model), key=lambda p: p.valid_from)
    if provider is not None:
        for row in rows:
            if row.provider != provider:
                storage.save_price(replace(row, provider=provider), by=by)
        rows = [replace(row, provider=provider) for row in rows]
    at = start or now
    base = price_at(rows, at)
    if base is None:
        kind = source or PriceSource.API
        new = ModelPrice(model, kind, start or now, provider=provider or "", **values)  # type: ignore[arg-type]
        storage.save_price(new, by=by)
        return new
    changed = replace(base, price_set=True, **values)  # type: ignore[arg-type]
    if source is not None:
        changed = replace(changed, source=source)
    if start is None or start == base.valid_from:
        storage.save_price(changed, by=by)
        return changed
    if start < base.valid_from:  # before the first row
        new = replace(changed, valid_from=start, valid_to=base.valid_from)
        storage.save_price(new, by=by)
        return new
    storage.save_price(replace(base, valid_to=start), by=by)
    new = replace(changed, valid_from=start, valid_to=base.valid_to)
    storage.save_price(new, by=by)
    return new


def ensure_price_row(
    storage: StorageWriter,
    model: str,
    *,
    local: bool,
    ts: datetime,
    by: str,
    provider: str = "",
) -> bool:
    """Create the default row for a model seen for the first time; True if created."""
    if storage.get_prices(model):
        return False
    source = PriceSource.LOCAL if local else PriceSource.API
    storage.save_price(ModelPrice(model, source, ts, price_set=local, provider=provider), by=by)
    return True


@dataclass(frozen=True, slots=True)
class Applied:
    model: str
    action: str  # "added", "priced", "new period" or "unchanged"
    old: tuple[Decimal, ...] | None
    new: tuple[Decimal, ...]


def apply_reference(
    storage: StorageWriter,
    reference: ReferenceList,
    *,
    now: datetime,
    by: str,
    dry_run: bool = False,
) -> list[Applied]:
    """Apply the reference list to the price table.

    - a model with no row gets one;
    - a row still at "price not set" takes the reference price for its whole
      period (it was never priced, so nothing priced is rewritten);
    - a price the admin set that differs starts a new period now, so earlier
      days keep the price they had;
    - dated snapshots in the table (``claude-opus-4-1-20250805``) follow their
      base model.
    """
    targets: dict[str, ReferencePrice] = {r.model: r for r in reference.models}
    for price in storage.get_prices():
        found = reference.find(price.model)
        if found is not None:
            targets.setdefault(price.model, found)
    results = []
    for model, ref in sorted(targets.items()):
        values = {f: getattr(ref, f) for f in PRICE_FIELDS}
        new = tuple(values.values())
        current = price_at(storage.get_prices(model), now)
        if current is None:
            action = "added"
        elif not current.price_set:
            action = "priced"
        elif _values(current) == new:
            results.append(Applied(model, "unchanged", new, new))
            continue
        else:
            action = "new period"
        if not dry_run:
            set_price(
                storage,
                model,
                values,
                now=now,
                by=by,
                start=now if action == "new period" else None,
                source=PriceSource.API,
                provider=ref.provider,
            )
        results.append(Applied(model, action, None if current is None else _values(current), new))
    return results


__all__ = [
    "PRICE_FIELDS",
    "Applied",
    "ReferenceList",
    "ReferencePrice",
    "apply_reference",
    "base_model",
    "current_prices",
    "ensure_price_row",
    "load_reference",
    "price_at",
    "set_price",
]
