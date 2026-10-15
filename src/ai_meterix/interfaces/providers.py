"""Text output for ``aimeterix providers`` (record 0018)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from ai_meterix.core.model import ModelPrice, Provider
from ai_meterix.pricing.providers import app_url

from .prices import _money


def _priced(prices: Iterable[ModelPrice], name: str) -> list[ModelPrice]:
    return [p for p in prices if p.provider == name]


def render_providers(
    providers: Sequence[Provider], prices: Sequence[ModelPrice], proxy: str
) -> str:
    lines = ["Providers (apps call the address in the last column instead of the provider)"]
    rows: list[Sequence[str]] = [("Name", "Label", "Kind", "On", "Models priced", "App base URL")]
    for p in providers:
        priced = [m for m in _priced(prices, p.name) if m.price_set]
        rows.append(
            (
                p.name,
                p.label,
                p.kind.value,
                "yes" if p.enabled else "no",
                str(len(priced)),
                app_url(p, proxy),
            )
        )
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines.extend(
        "  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in rows
    )
    lines.append("")
    lines.append(
        "A model with no price costs 0. Set one with `aimeterix prices set MODEL --provider NAME`."
    )
    lines.append("Details and models: `aimeterix providers show NAME`.")
    return "\n".join(lines)


def render_provider(p: Provider, prices: Sequence[ModelPrice], proxy: str) -> str:
    lines = [
        f"{p.label} ({p.name}), {p.kind.value}, {'enabled' if p.enabled else 'disabled'}"
        + (", built in" if p.built_in else ""),
        f"  provider address  {p.base_url}{p.app_path}",
        f"  apps use          {app_url(p, proxy)}",
        f"  response format   {p.api_format}",
        "",
    ]
    models = _priced(prices, p.name)
    if not models:
        lines.append("No models yet: the proxy adds each model it sees, or set a price with")
        lines.append(f"`aimeterix prices set MODEL --provider {p.name} --input N --output N`.")
        return "\n".join(lines)
    rows: list[Sequence[str]] = [("Model", "Input", "Output", "Cache read", "Cache write")]
    for m in models:
        if not m.price_set:
            rows.append((m.model, "not set", "-", "-", "-"))
            continue
        rows.append(
            (
                m.model,
                _money(m.input_per_m),
                _money(m.output_per_m),
                _money(m.cache_read_per_m),
                _money(m.cache_write_per_m),
            )
        )
    lines.append("Prices per 1M tokens in USD, in force now")
    lines.extend(_table_one(rows))
    return "\n".join(lines)


def _table_one(rows: Sequence[Sequence[str]]) -> list[str]:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return [
        "  ".join(
            c.ljust(w) if i == 0 else c.rjust(w)
            for i, (c, w) in enumerate(zip(r, widths, strict=True))
        ).rstrip()
        for r in rows
    ]


__all__ = ["render_provider", "render_providers"]
