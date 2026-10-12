"""Platform probe and ``aimeterix doctor`` (record 0009).

The macOS checks run on readings recorded on a real Mac (tests/fixtures), so
they run on any machine.
"""

from __future__ import annotations

import json
import platform
from pathlib import Path
from typing import Any

import pytest

from ai_meterix.collectors import probe as probe_mod
from ai_meterix.collectors.macos.probe import (
    BACKEND,
    RecordingReaders,
    ReplayReaders,
    energy_by_part,
    probe_macos,
)
from ai_meterix.collectors.probe import METRICS, MetricCheck, Support, run_check
from ai_meterix.core.model import PoolType
from ai_meterix.interfaces import cli
from ai_meterix.interfaces.doctor import render

FIXTURES = Path(__file__).parent.parent / "fixtures"


def recorded(name: str = "doctor-m4.json") -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return data["readings"]  # type: ignore[no-any-return]


def mac_report(readings: dict[str, Any] | None = None) -> probe_mod.ProbeReport:
    replay = ReplayReaders(readings if readings is not None else recorded())
    return probe_macos(replay, interval_s=0.001, hostname="test-mac")


# --- macOS, replayed --------------------------------------------------------


def test_mac_platform_info_from_recorded_readings() -> None:
    info = mac_report().platform
    assert info.os == "macos"
    assert info.arch == "arm64"
    assert info.backend == BACKEND
    assert info.hostname == "test-mac"
    assert info.chip and info.chip.startswith("Apple M")
    assert info.memory_type is PoolType.UNIFIED
    assert info.memory_bytes and info.memory_bytes > 0
    assert sum(c.cores for c in info.clusters) == info.cpu_cores
    assert {c.name for c in info.clusters} == {"Performance", "Efficiency"}


def test_mac_reports_every_metric_in_order() -> None:
    report = mac_report()
    assert [c.metric for c in report.checks] == list(METRICS)


def test_mac_core_metrics_supported_with_readings() -> None:
    report = mac_report()
    for metric in ("cpu.util", "cpu.clusters", "memory.used", "memory.pressure", "thermal.state"):
        check = report.check(metric)
        assert check.support is Support.SUPPORTED, check
        assert check.value, check


def test_gpu_memory_is_partial_on_unified_memory() -> None:
    check = mac_report().check("gpu.memory")
    assert check.support is Support.PARTIAL
    assert check.reason and "unified" in check.reason


def test_readings_round_trip() -> None:
    """A probe over replayed readings records the same readings again."""
    first = mac_report()
    second = mac_report(first.readings)
    assert second.to_dict()["checks"] == first.to_dict()["checks"]


def test_missing_source_becomes_unsupported_with_reason() -> None:
    readings = recorded()
    readings["energy"] = [{"error": "IOReport has no 'Energy Model' channels on this Mac"}]
    readings["gpu"] = [[]]
    report = mac_report(readings)
    for metric in ("cpu.power", "gpu.power", "npu.power"):
        check = report.check(metric)
        assert check.support is Support.UNSUPPORTED
        assert check.reason == "IOReport has no 'Energy Model' channels on this Mac"
        assert check.value is None
    assert report.check("gpu.util").support is Support.UNSUPPORTED
    assert report.check("gpu.util").reason == "no GPU found in IOKit (IOAccelerator)"


def test_unknown_cluster_layout_is_partial() -> None:
    readings = recorded()
    readings["cpu_layout"] = [[]]
    check = mac_report(readings).check("cpu.clusters")
    assert check.support is Support.PARTIAL
    assert check.reason


def test_unreadable_system_processes_make_cpu_time_partial() -> None:
    readings = recorded()
    readings["processes"] = [{"listed": 500, "readable": 320, "own_cpu_s": 0.5}]
    check = mac_report(readings).check("process.cpu_time")
    assert check.support is Support.PARTIAL
    assert check.value == "320 of 500 processes readable"


def test_unknown_pressure_level_is_unsupported() -> None:
    readings = recorded()
    readings["sysctl_int"] = {**readings["sysctl_int"], "kern.memorystatus_vm_pressure_level": 7}
    assert mac_report(readings).check("memory.pressure").support is Support.UNSUPPORTED


def test_rosetta_is_noted() -> None:
    readings = recorded()
    readings["sysctl_int"] = {**readings["sysctl_int"], "sysctl.proc_translated": 1}
    assert any("Rosetta" in note for note in mac_report(readings).platform.notes)


def test_energy_channels_grouped_by_part() -> None:
    parts = energy_by_part({"CPU Energy": 2.0, "GPU Energy": 1.0, "ANE0": 0.25, "DRAM0": 9.0})
    assert parts == {"cpu": 2.0, "gpu": 1.0, "npu": 0.25}
    split = energy_by_part({"EACC_CPU Energy": 0.5, "PACC0_CPU Energy": 1.5})
    assert split == {"cpu": 2.0}
    assert energy_by_part({}) == {}


def test_recording_keeps_errors_and_reraises() -> None:
    class Broken:
        def thermal_level(self) -> int:
            raise OSError("no thermal")

        def sysctl_str(self, name: str) -> str:
            return "x"

    rec = RecordingReaders(Broken())
    with pytest.raises(OSError, match="no thermal"):
        rec.thermal_level()
    assert rec.sysctl_str("hw.model") == "x"
    assert rec.readings == {
        "thermal_level": [{"error": "no thermal"}],
        "sysctl_str": {"hw.model": "x"},
    }


def test_replay_without_reading_raises() -> None:
    with pytest.raises(OSError, match="no recorded swap"):
        ReplayReaders({}).swap()


def test_readings_carry_no_process_names() -> None:
    text = json.dumps(recorded())
    for client in recorded().get("gpu_clients", [[]])[0]:
        assert set(client) <= {"pid", "gpu_ns"}
    assert "IOUserClientCreator" not in text


# --- generic and other platforms -------------------------------------------


def test_run_check_turns_errors_into_unsupported() -> None:
    def boom() -> MetricCheck:
        raise OSError("sensor missing")

    check = run_check("gpu.util", boom)
    assert check == MetricCheck("gpu.util", Support.UNSUPPORTED, reason="sensor missing")
    assert check.label == "GPU use"


@pytest.mark.parametrize(
    ("system", "machine", "reason"),
    [
        ("Linux", "x86_64", "no Linux backend yet"),
        ("Darwin", "x86_64", "Intel Macs are not supported"),
        ("Windows", "AMD64", "Windows is not supported yet"),
    ],
)
def test_other_platforms_report_everything_unsupported(
    monkeypatch: pytest.MonkeyPatch, system: str, machine: str, reason: str
) -> None:
    monkeypatch.setattr(platform, "system", lambda: system)
    monkeypatch.setattr(platform, "machine", lambda: machine)
    report = probe_mod.probe()
    assert report.platform.backend is None
    assert len(report.checks) == len(METRICS)
    assert all(c.support is Support.UNSUPPORTED for c in report.checks)
    assert all(c.reason and reason in c.reason for c in report.checks)


# --- doctor output -----------------------------------------------------------


def test_doctor_text_lists_machine_and_every_metric() -> None:
    report = mac_report()
    text = render(report)
    assert report.platform.chip and report.platform.chip in text
    for label in METRICS.values():
        assert label in text
    counts = report.counts()
    assert f"{counts[Support.SUPPORTED]} supported" in text
    assert "never as zero" in text


def test_doctor_json_is_serialisable_and_optional_readings() -> None:
    report = mac_report()
    plain = report.to_dict()
    assert "readings" not in plain
    assert plain["platform"]["memory_type"] == "unified"
    assert {c["support"] for c in plain["checks"]} <= {s.value for s in Support}
    json.dumps(report.to_dict(readings=True))


def test_cli_doctor_exit_code(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    real_probe = probe_mod.probe
    monkeypatch.setattr(probe_mod, "probe", lambda: mac_report())
    assert cli.main(["doctor"]) == 0
    assert "AI Meterix doctor" in capsys.readouterr().out
    assert cli.main(["doctor", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["platform"]["os"] == "macos"

    monkeypatch.setattr(probe_mod, "probe", real_probe)
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    assert cli.main(["doctor"]) == 1


def test_mac_power_from_recorded_channels() -> None:
    report = mac_report()
    for metric in ("cpu.power", "gpu.power"):
        assert report.check(metric).support is Support.SUPPORTED
    assert report.check("npu.power").support is Support.PARTIAL


def test_cli_without_command_prints_help(capsys: Any) -> None:
    assert cli.main([]) == 2
    assert "doctor" in capsys.readouterr().out
