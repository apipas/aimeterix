"""The usage statement as a table or CSV (PLAN.md Phase 3 step 5).

Columns follow provider usage pages: requests, input, output, cache read,
cache write and total tokens per model, then energy and cost. Energy is shown
in Wh, which reads better at desk scale; the CSV has both Wh and kWh, at full
precision. Prices stay per kWh. A count the
provider does not report is "-", never 0.
"""

from __future__ import annotations

import csv
import io
from decimal import ROUND_HALF_UP, Decimal

from ai_meterix.pricing.engine import ESTIMATE, PRICE_NOT_SET, TOKEN_FIELDS, Statement, UsageLine

_HEAD = ("Requests", "Input", "Output", "Cache read", "Cache write", "Total", "Wh", "Cost")
_MARKS = {PRICE_NOT_SET: "!", ESTIMATE: "~"}


def _lines(statement: Statement, by: str) -> list[UsageLine]:
    return statement.by_model() if by == "model" else statement.lines


def _count(value: int | None) -> str:
    return "-" if value is None else f"{value:,}"


def _money(value: Decimal, places: str = "0.0001") -> str:
    return str(value.quantize(Decimal(places), rounding=ROUND_HALF_UP))


def _source(line: UsageLine) -> str:
    return line.provider or line.source or ""


def render_table(statement: Statement, by: str = "day") -> str:
    lines = _lines(statement, by)
    currency = statement.currency or ""
    rows: list[tuple[str, ...]] = []
    for line in lines:
        marks = "".join(mark for note, mark in _MARKS.items() if note in line.notes)
        rows.append(
            (
                line.day.isoformat() if by == "day" else "",
                line.model,
                _source(line),
                f"{line.requests:,}" if line.requests else "",
                *(_count(getattr(line, name)) for name in TOKEN_FIELDS),
                _count(line.total_tokens),
                _money(line.kwh * 1000, "0.1"),
                _money(line.cost) + marks,
            )
        )
    total = statement.total()
    rows.append(
        (
            "",
            "Total",
            "",
            f"{total.requests:,}",
            *(_count(getattr(total, name)) for name in TOKEN_FIELDS),
            _count(total.total_tokens),
            _money(total.kwh * 1000, "0.1"),
            _money(total.cost),
        )
    )
    head = (
        "Day" if by == "day" else "",
        "Model",
        "Source",
        *_HEAD[:-1],
        f"Cost {currency}".strip(),
    )
    widths = [max(len(r[i]) for r in [head, *rows]) for i in range(len(head))]
    if by != "day":
        widths[0] = 0

    def fmt(row: tuple[str, ...]) -> str:
        cells = [
            cell.ljust(widths[i]) if i < 3 else cell.rjust(widths[i])
            for i, cell in enumerate(row)
            if widths[i]
        ]
        return "  ".join(cells).rstrip()

    out = [
        f"Usage {statement.start:%Y-%m-%d %H:%M} to {statement.end:%Y-%m-%d %H:%M} UTC"
        f" (days in {statement.tz})",
        "",
        fmt(head),
    ]
    out += [fmt(r) for r in rows[:-1]]
    out.append("-" * len(fmt(head)))
    out.append(fmt(rows[-1]))
    notes = [
        "'-' = not reported by the provider (not 0)",
        "~ = local model cost from its energy share (an estimate)",
    ]
    if any(PRICE_NOT_SET in line.notes for line in lines):
        notes.append("! = price not set: shown as 0 until an admin sets it")
    if statement.currency is None:
        notes.append("no cost profile: energy is shown, but not priced")
    out += ["", *notes]
    return "\n".join(out)


def render_csv(statement: Statement, by: str = "day") -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    columns = [
        "day",
        "model",
        "source",
        "provider",
        "requests",
        *TOKEN_FIELDS,
        "total_tokens",
        "wh",
        "kwh",
        "energy_cost",
        "fee_cost",
        "token_cost",
        "cost",
        "currency",
        "notes",
    ]
    writer.writerow(columns)
    for line in _lines(statement, by):
        d = line.as_dict()
        d["currency"] = statement.currency or ""
        d["wh"] = line.kwh * 1000
        d["notes"] = "; ".join(d["notes"])
        writer.writerow(["" if d[c] is None else d[c] for c in columns])
    return buffer.getvalue()
