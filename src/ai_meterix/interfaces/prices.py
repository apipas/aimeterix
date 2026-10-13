"""Text output for ``aimeterix prices`` (record 0012 D3.7)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import tzinfo
from decimal import Decimal

from ai_meterix.core.model import ModelPrice, PriceChange
from ai_meterix.pricing.prices import Applied, ReferenceList

_HEADER = ("Model", "Source", "Input", "Output", "Cache read", "Cache write", "Since")


def _money(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _table(rows: Sequence[Sequence[str]]) -> list[str]:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    out = []
    for row in rows:
        cells = [
            c.ljust(w) if i < 2 else c.rjust(w)
            for i, (c, w) in enumerate(zip(row, widths, strict=True))
        ]
        out.append("  ".join(cells).rstrip())
    return out


def render_prices(
    prices: Iterable[ModelPrice], currency: str | None, tz: tzinfo, reference: ReferenceList
) -> str:
    unit = currency or "your profile's currency"
    lines = [f"Prices per 1M tokens in {unit}, in force now"]
    rows: list[Sequence[str]] = [_HEADER]
    unset = False
    for p in prices:
        since = p.valid_from.astimezone(tz).strftime("%Y-%m-%d")
        if p.source.value == "api" and not p.price_set:
            unset = True
            rows.append((p.model, p.source.value, "not set", "-", "-", "-", since))
            continue
        rows.append(
            (
                p.model,
                p.source.value,
                _money(p.input_per_m),
                _money(p.output_per_m),
                _money(p.cache_read_per_m),
                _money(p.cache_write_per_m),
                since,
            )
        )
    if len(rows) == 1:
        lines.append("  no prices yet: the proxy adds a row for each model it sees")
    else:
        lines.extend(_table(rows))
    lines.append("")
    if unset:
        lines.append("'not set' = an API model nobody priced yet; its cost shows as 0, marked")
    lines.append(
        f"Reference list: {len(reference.models)} OpenAI and Anthropic models, "
        f"checked {reference.checked}, in {reference.currency}. "
        "See `aimeterix prices reference`."
    )
    return "\n".join(lines)


def render_reference(reference: ReferenceList) -> str:
    lines = [
        f"Reference prices per 1M tokens in {reference.currency}, "
        f"checked {reference.checked} (not applied until you run "
        "`aimeterix prices apply-reference`)"
    ]
    rows: list[Sequence[str]] = [_HEADER[:-1]]
    for r in reference.models:
        rows.append(
            (
                r.model,
                r.provider,
                _money(r.input_per_m),
                _money(r.output_per_m),
                _money(r.cache_read_per_m),
                _money(r.cache_write_per_m),
            )
        )
    lines.extend(_table(rows))
    lines.append("")
    lines.extend(f"- {note}" for note in reference.notes)
    lines.extend(f"- source: {url}" for url in reference.sources.values())
    lines.append("- prices change often: check the sources before relying on them")
    return "\n".join(lines)


def render_applied(results: Sequence[Applied], dry_run: bool) -> str:
    changed = [r for r in results if r.action != "unchanged"]
    verb = "would change" if dry_run else "changed"
    lines = [f"Reference list: {len(changed)} {verb}, {len(results) - len(changed)} unchanged"]
    for r in changed:
        new = "/".join(_money(v) for v in r.new)
        old = "" if r.old is None else " (was " + "/".join(_money(v) for v in r.old) + ")"
        lines.append(f"  {r.action:<10}  {r.model}  {new}{old}")
    if changed:
        lines.append("  prices are input/output/cache read/cache write per 1M tokens")
    if dry_run and changed:
        lines.append("Nothing saved; run again without --dry-run to apply.")
    return "\n".join(lines)


def render_history(changes: Iterable[PriceChange], tz: tzinfo) -> str:
    lines = []
    for c in changes:
        ts = c.ts.astimezone(tz).strftime("%Y-%m-%d %H:%M")
        lines.append(f"{ts}  {c.by:<10}  {c.model}  {c.field_name}: {c.old or '-'} -> {c.new}")
    return "\n".join(lines) if lines else "no price changes yet"
