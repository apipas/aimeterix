"""``aimeterix doctor``: what this machine can measure, and why not when it can't."""

from __future__ import annotations

from ai_meterix.collectors.probe import ProbeReport, Support


def _gb(value: int) -> str:
    return f"{value / 2**30:.0f} GB"


def render(report: ProbeReport) -> str:
    info = report.platform
    lines = ["AI Meterix doctor", ""]
    machine = info.chip or "unknown chip"
    if info.model:
        machine += f" ({info.model})"
    lines.append(f"Machine   {machine}, {info.os} {info.os_version}, {info.arch}")
    if info.cpu_cores:
        cpu = f"{info.cpu_cores} cores"
        if len(info.clusters) > 1:
            cpu += ": " + ", ".join(f"{c.cores} {c.name}" for c in info.clusters)
        lines.append(f"CPU       {cpu}")
    if info.gpu:
        gpu = info.gpu + (f", {info.gpu_cores} cores" if info.gpu_cores else "")
        lines.append(f"GPU       {gpu}")
    if info.memory_bytes:
        kind = f" {info.memory_type.value}" if info.memory_type else ""
        lines.append(f"Memory    {_gb(info.memory_bytes)}{kind}")
    lines.append(f"Backend   {info.backend or 'none for this platform yet'}")
    lines.append("")

    rows = [("Metric", "Support", "Reading", "Source / reason")]
    for check in report.checks:
        detail = check.source or ""
        if check.reason:
            detail = f"{detail}: {check.reason}" if detail else check.reason
        rows.append((check.label, check.support.value, check.value or "-", detail))
    widths = [max(len(row[i]) for row in rows) for i in range(3)]
    for row in rows:
        lines.append(
            "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row[:3])) + "  " + row[3]
        )

    if info.notes:
        lines.append("")
        lines.extend(f"Note: {note}" for note in info.notes)
    counts = report.counts()
    lines.append("")
    lines.append(
        f"{len(report.checks)} metrics: {counts[Support.SUPPORTED]} supported, "
        f"{counts[Support.PARTIAL]} partial, {counts[Support.UNSUPPORTED]} unsupported."
    )
    lines.append("Unsupported metrics are stored as missing, never as zero.")
    return "\n".join(line.rstrip() for line in lines)
