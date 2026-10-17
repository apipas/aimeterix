"""``aimeterix init``, ``uninstall`` and ``paths`` (records 0016, 0017 and 0019)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_meterix.core.paths import Paths, paths
from ai_meterix.interfaces.cli import main
from ai_meterix.interfaces.setup import (
    purge,
    removal_hint,
    safe_url,
    set_value,
    starter_text,
)
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


def answers(monkeypatch: pytest.MonkeyPatch, *replies: str) -> None:
    queue = list(replies)
    monkeypatch.setattr("builtins.input", lambda _: queue.pop(0))


def test_init_writes_the_defaults_then_creates_the_database(
    where: Paths,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers(monkeypatch, "n", "n")  # change nothing, create nothing
    assert main(["init"]) == 1
    out = capsys.readouterr().out
    assert where.config_file.read_text() == starter_text()
    assert "with the default settings" in out and "Nothing changed" in out
    assert not where.db_file.exists()
    assert list(tmp_path.iterdir()) == []  # nothing in the folder the command ran from

    assert main(["init", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Database created" in out and "aimeterix start" in out and "/openai/v1" in out
    with open_reader(where.storage_url) as storage:
        assert storage.get_user("admin") is not None and storage.get_user("user") is not None
        assert len(storage.get_providers()) == 10
        assert "agent.interval_s" in storage.get_settings()

    assert main(["init", "--yes"]) == 1  # never twice
    assert "already set up" in capsys.readouterr().err


def test_init_lets_you_change_the_defaults(
    where: Paths, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # change? proxy port, local server, dashboard port, interval, database; create?
    answers(monkeypatch, "y", "8080", "http://127.0.0.1:8081", "", "", "", "y")
    assert main(["init"]) == 0
    text = where.config_file.read_text()
    assert "  port: 8080\n" in text and "  default_upstream: http://127.0.0.1:8081\n" in text
    assert "  port: 8788\n" in text and "# Ten cloud providers" in text  # the rest is kept
    assert "Saved in" in capsys.readouterr().out
    with open_reader(where.storage_url) as storage:
        settings = storage.get_settings()
        assert settings["proxy.default_upstream"].value == "http://127.0.0.1:8081"


def test_a_bad_answer_leaves_the_file_as_it_was(
    where: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers(monkeypatch, "n", "n")
    main(["init"])
    answers(monkeypatch, "y", "", "", "", "0", "")  # interval 0 is out of range
    assert main(["init"]) == 2
    assert where.config_file.read_text() == starter_text()
    assert not where.db_file.exists()


def test_set_value_keeps_comments_and_switches_on_a_commented_key() -> None:
    text = "proxy:\n  port: 8787    # the port\n  # default_upstream: http://x\napi:\n  port: 1\n"
    text = set_value(text, "proxy", "port", "9000")
    text = set_value(text, "proxy", "default_upstream", "http://y")
    text = set_value(text, "proxy", "host", "127.0.0.1")
    text = set_value(text, "storage", "url", "sqlite:///m.db")
    assert text == (
        "proxy:\n  host: 127.0.0.1\n  port: 9000    # the port\n  default_upstream: http://y\n"
        "api:\n  port: 1\n\nstorage:\n  url: sqlite:///m.db\n"
    )


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
    def no_terminal(_: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", no_terminal)
    assert main(["init"]) == 1
    assert where.config_file.exists() and not where.db_file.exists()


def test_uninstall_keeps_the_data_unless_purged(
    where: Paths, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
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


@pytest.mark.parametrize(
    ("kind", "hint"),
    [
        ("deb", "sudo apt remove ai-meterix"),
        ("pkg", "pkgutil --forget"),
        ("script", "rm -rf"),
        (None, "pip uninstall ai-meterix"),
    ],
)
def test_uninstall_says_how_the_program_was_installed(
    tmp_path: Path, kind: str | None, hint: str
) -> None:
    (tmp_path / "python").mkdir()
    if kind:
        (tmp_path / "INSTALLED_BY").write_text(f"{kind}\n")
    assert hint in removal_hint(str(tmp_path / "python"))


def test_passwords_never_show_in_the_summary() -> None:
    assert safe_url("postgresql://me:secret@db:5432/m") == "postgresql://me:***@db:5432/m"
    assert safe_url("sqlite:///x.db") == "sqlite:///x.db"
