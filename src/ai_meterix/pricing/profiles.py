"""Saving a host's cost profile (record 0012 D3.2), shared by the CLI and the API.

Money is in USD for now (``CURRENCY``). A profile saved earlier in another
currency must be given its rate, and its fees when it has any, again in USD,
so no number is silently reinterpreted.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from ai_meterix.core.model import CURRENCY, CostProfile, FeeSpread
from ai_meterix.storage.base import StorageWriter

_NUMBERS = {"rate": "energy_rate_per_kwh", "pue": "pue", "fees": "fixed_fees_per_month"}


def set_profile(
    storage: StorageWriter, host: str, changes: Mapping[str, Any], *, by: str
) -> CostProfile:
    """Save a new profile version from ``changes`` (rate, pue, fees, spread, name, region).

    Raises ``ValueError`` with a message for the user when the input is wrong.
    """
    unknown = set(changes) - {*_NUMBERS, "spread", "name", "region"}
    if unknown:
        raise ValueError(f"unknown profile fields: {', '.join(sorted(unknown))}")
    numbers: dict[str, Decimal] = {}
    for key, field in _NUMBERS.items():
        if changes.get(key) is None:
            continue
        try:
            numbers[field] = Decimal(str(changes[key]))
        except InvalidOperation:
            raise ValueError("rate, pue and fees take numbers") from None
    current = storage.get_profile(host)
    if current is None and "energy_rate_per_kwh" not in numbers:
        raise ValueError("the first profile needs a rate")
    if current is not None and current.currency != CURRENCY:
        needed = ["rate"] + (["fees"] if current.fixed_fees_per_month else [])
        missing = [n for n in needed if _NUMBERS[n] not in numbers]
        if missing:
            raise ValueError(
                f"your profile is in {current.currency}, and AI Meterix uses {CURRENCY} "
                f"for now; give {' and '.join(missing)} in {CURRENCY}"
            )
    base = current or CostProfile(host, "default", CURRENCY, Decimal(0))
    spread = changes.get("spread")
    try:
        profile = CostProfile(
            host,
            changes.get("name") or base.name,
            CURRENCY,
            numbers.get("energy_rate_per_kwh", base.energy_rate_per_kwh),
            region=changes["region"] if changes.get("region") is not None else base.region,
            pue=numbers.get("pue", base.pue),
            fixed_fees_per_month=numbers.get("fixed_fees_per_month", base.fixed_fees_per_month),
            fee_spread=FeeSpread(spread) if spread else base.fee_spread,
        )
    except ValueError as exc:  # also the model's ValidationError
        raise ValueError(str(exc)) from None
    return storage.save_profile(profile, by=by)


def profile_dict(profile: CostProfile) -> dict[str, Any]:
    return {
        "host": profile.host_id,
        "name": profile.name,
        "currency": profile.currency,
        "rate_per_kwh": profile.energy_rate_per_kwh,
        "pue": profile.pue,
        "fees_per_month": profile.fixed_fees_per_month,
        "fee_spread": profile.fee_spread.value,
        "region": profile.region,
        "version": profile.version,
        "saved_at": profile.saved_at,
    }


__all__ = ["profile_dict", "set_profile"]
