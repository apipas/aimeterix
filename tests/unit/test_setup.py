"""``aimeterix init`` and ``aimeterix uninstall`` (records 0016 and 0017)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_meterix.interfaces.cli import main
from ai_meterix.interfaces.setup import safe_url, starter_text
from ai_meterix.storage import open_reader


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AIMETERIX_CONFIG", raising=False)
    monkeypatch.delenv("AIMETERIX_STORAGE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_the_example_file_is_the_starter_file() -> None:
    root = Path(__file__).resolve().parents[2]
    assert (root / "examples" / "aimeterix.example.yaml").read_text() == starter_text()


def test_init_writes_a_starter_file_then_creates_the_database(
    home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert main(["init"]) == 0
    assert (home / "aimeterix.yaml").read_text() == starter_text()
    assert "run `aimeterix init` again" in capsys.readouterr().out
    assert not (home / ".aimeterix").exists()  # nothing created yet

    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert main(["init"]) == 1
    assert "Nothing changed" in capsys.readouterr().out
    assert not (home / ".aimeterix" / "meter.db").exists()

    assert main(["init", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Database created" in out and "aimeterix start" in out and "/openai/v1" in out
    with open_reader(f"sqlite:///{home / '.aimeterix' / 'meter.db'}") as storage:
        assert storage.get_user("admin") is not None and storage.get_user("user") is not None
        assert len(storage.get_providers()) == 10
        assert "agent.interval_s" in storage.get_settings()

    assert main(["init", "--yes"]) == 1  # never twice
    assert "already set up" in capsys.readouterr().err


def test_init_refuses_a_bad_config_file(home: Path) -> None:
    (home / "aimeterix.yaml").write_text("agent:\n  interval_s: 0\n")
    assert main(["init", "--yes"]) == 2


def test_init_never_assumes_yes_without_a_terminal(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (home / "aimeterix.yaml").write_text(starter_text())

    def no_terminal(_: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", no_terminal)
    assert main(["init"]) == 1


def test_uninstall_keeps_the_data_unless_purged(
    home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    (home / "aimeterix.yaml").write_text(starter_text())
    assert main(["init", "--yes"]) == 0
    capsys.readouterr()
    assert main(["uninstall"]) == 0
    assert (home / ".aimeterix" / "meter.db").exists()
    assert "uninstall --purge" in capsys.readouterr().out
    monkeypatch.setattr("builtins.input", lambda _: "no")
    assert main(["uninstall", "--purge"]) == 1
    assert (home / ".aimeterix").exists()
    assert main(["uninstall", "--purge", "--yes"]) == 0
    assert not (home / ".aimeterix").exists()
    assert "pip uninstall ai-meterix" in capsys.readouterr().out


def test_uninstall_names_a_database_kept_elsewhere(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (home / "aimeterix.yaml").write_text(
        f"storage:\n  url: sqlite:///{tmp_path / 'data' / 'm.db'}\n"
    )
    (tmp_path / "data").mkdir()
    assert main(["init", "--yes"]) == 0
    assert main(["uninstall", "--purge", "--yes"]) == 0
    assert "was not deleted" in capsys.readouterr().out
    assert (tmp_path / "data" / "m.db").exists()


def test_passwords_never_show_in_the_summary() -> None:
    assert safe_url("postgresql://me:secret@db:5432/m") == "postgresql://me:***@db:5432/m"
    assert safe_url("sqlite:///x.db") == "sqlite:///x.db"
