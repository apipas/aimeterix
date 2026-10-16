"""``aimeterix init``, ``uninstall`` and ``paths`` (records 0016, 0017 and 0019)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_meterix.core.paths import Paths, paths
from ai_meterix.interfaces.cli import main
from ai_meterix.interfaces.setup import purge, safe_url, starter_text
from ai_meterix.storage import open_reader


@pytest.fixture
def where(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Paths:
    """The default folders of a fresh user, run from an unrelated folder."""
    monkeypatch.delenv("AIMETERIX_STORAGE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    return paths()


def test_the_example_file_is_the_starter_file() -> None:
    root = Path(__file__).resolve().parents[2]
    assert (root / "examples" / "aimeterix.example.yaml").read_text() == starter_text()


def test_init_writes_a_starter_file_then_creates_the_database(
    where: Paths,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert main(["init"]) == 0
    assert where.config_file.read_text() == starter_text()
    assert "run `aimeterix init` again" in capsys.readouterr().out
    assert not where.db_file.exists()  # nothing created yet
    assert list(tmp_path.iterdir()) == []  # nothing in the folder the command ran from

    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert main(["init"]) == 1
    assert "Nothing changed" in capsys.readouterr().out
    assert not where.db_file.exists()

    assert main(["init", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Database created" in out and "aimeterix start" in out and "/openai/v1" in out
    with open_reader(where.storage_url) as storage:
        assert storage.get_user("admin") is not None and storage.get_user("user") is not None
        assert len(storage.get_providers()) == 10
        assert "agent.interval_s" in storage.get_settings()

    assert main(["init", "--yes"]) == 1  # never twice
    assert "already set up" in capsys.readouterr().err


def test_every_command_uses_the_same_config_from_any_folder(
    where: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_meterix.core.config import load_config

    where.config_dir.mkdir(parents=True)
    where.config_file.write_text("agent:\n  interval_s: 20\n")
    (where.config_dir / ".env").write_text("AIMETERIX_PROXY_PORT=8080\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "aimeterix.yaml").write_text("agent:\n  interval_s: 99\n")  # ignored
    monkeypatch.chdir(elsewhere)
    config = load_config()
    assert (config.agent.interval_s, config.proxy.port) == (20, 8080)


def test_init_refuses_a_bad_config_file(where: Paths) -> None:
    where.config_dir.mkdir(parents=True)
    where.config_file.write_text("agent:\n  interval_s: 0\n")
    assert main(["init", "--yes"]) == 2


def test_init_never_assumes_yes_without_a_terminal(
    where: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert main(["init"]) == 0

    def no_terminal(_: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", no_terminal)
    assert main(["init"]) == 1


def test_uninstall_keeps_the_data_unless_purged(
    where: Paths, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert main(["init"]) == 0
    assert main(["init", "--yes"]) == 0
    capsys.readouterr()
    assert main(["uninstall"]) == 0
    assert where.db_file.exists()
    assert "uninstall --purge" in capsys.readouterr().out
    monkeypatch.setattr("builtins.input", lambda _: "no")
    assert main(["uninstall", "--purge"]) == 1
    assert where.config_file.exists() and where.db_file.exists()
    assert main(["uninstall", "--purge", "--yes"]) == 0
    assert not any(f.exists() for f in where.folders())
    assert "pip uninstall ai-meterix" in capsys.readouterr().out


def test_uninstall_names_a_database_kept_elsewhere(
    where: Paths, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    where.config_dir.mkdir(parents=True)
    where.config_file.write_text(f"storage:\n  url: sqlite:///{tmp_path / 'data' / 'm.db'}\n")
    (tmp_path / "data").mkdir()
    assert main(["init", "--yes"]) == 0
    assert main(["uninstall", "--purge", "--yes"]) == 0
    assert "was not deleted" in capsys.readouterr().out
    assert (tmp_path / "data" / "m.db").exists()


def test_purge_never_deletes_the_home_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIMETERIX_HOME", str(Path.home()))
    with pytest.raises(ValueError, match="home folder"):
        purge()
    assert Path.home().exists()


def test_paths_shows_every_folder_and_the_old_one(
    where: Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    (Path.home() / ".aimeterix").mkdir()
    assert main(["paths"]) == 0
    out = capsys.readouterr().out
    assert str(where.config_file) in out and str(where.log_dir) in out
    assert "not created yet" in out and "before 0.1.0" in out


def test_passwords_never_show_in_the_summary() -> None:
    assert safe_url("postgresql://me:secret@db:5432/m") == "postgresql://me:***@db:5432/m"
    assert safe_url("sqlite:///x.db") == "sqlite:///x.db"
