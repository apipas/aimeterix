from pathlib import Path

import pytest

from ai_meterix.core.config import load_config, read_dotenv
from ai_meterix.core.errors import ConfigError
from ai_meterix.core.paths import paths

EXAMPLE = Path(__file__).parents[2] / "examples" / "aimeterix.example.yaml"


def write(tmp_path: Path, text: str, name: str = "aimeterix.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_without_a_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    config = load_config(env={})
    assert config.storage.url == paths().storage_url
    assert config.storage.raw_retention_days == 30
    assert config.storage.rollup_retention_days == 365
    assert config.agent.interval_s == 10
    assert config.api.host == "127.0.0.1"


def test_example_file_is_valid() -> None:
    config = load_config(EXAMPLE, env={})
    assert config.proxy.port == 8787


def test_explicit_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml", env={})


def test_config_path_from_environment(tmp_path: Path) -> None:
    path = write(tmp_path, "agent:\n  interval_s: 30\n", "custom.yaml")
    assert load_config(env={"AIMETERIX_CONFIG": str(path)}).agent.interval_s == 30


def test_environment_overrides_file(tmp_path: Path) -> None:
    path = write(tmp_path, "agent:\n  interval_s: 30\nstorage:\n  raw_retention_days: 7\n")
    config = load_config(
        path, env={"AIMETERIX_AGENT_INTERVAL_S": "5", "AIMETERIX_STORAGE_URL": "duckdb:///x"}
    )
    assert config.agent.interval_s == 5
    assert config.storage.url == "duckdb:///x"
    assert config.storage.raw_retention_days == 7


def test_dotenv_next_to_file_is_read_and_environment_wins(tmp_path: Path) -> None:
    path = write(tmp_path, "")
    write(
        tmp_path,
        "# secrets\nexport AIMETERIX_STORAGE_URL='postgresql://u:secret@db/meter'\n"
        "AIMETERIX_API_PORT=9000\n",
        ".env",
    )
    config = load_config(path, env={"AIMETERIX_API_PORT": "9100"})
    assert config.storage.url == "postgresql://u:secret@db/meter"
    assert config.api.port == 9100


def test_password_in_file_is_refused(tmp_path: Path) -> None:
    path = write(tmp_path, "storage:\n  url: postgresql://u:secret@db/meter\n")
    with pytest.raises(ConfigError, match="contains a password") as info:
        load_config(path, env={})
    assert "secret" not in str(info.value).replace("secrets", "")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("agent:\n  interval_s: 0.5\n", "agent.interval_s"),
        ("agent:\n  interval_s: 301\n", "agent.interval_s"),
        ("api:\n  port: 70000\n", "api.port"),
        ("storage:\n  colour: blue\n", "storage.colour"),
        ("- a\n- b\n", "top level must be a mapping"),
        ("agent: [1\n", "invalid YAML"),
        ("agent: 5\n", "agent"),
    ],
)
def test_invalid_files_are_explained(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, text), env={})


def test_env_override_is_validated(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"agent\.interval_s"):
        load_config(write(tmp_path, ""), env={"AIMETERIX_AGENT_INTERVAL_S": "fast"})


def test_config_is_immutable(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, ""), env={})
    with pytest.raises(Exception):  # noqa: B017 - pydantic raises its own frozen error
        config.agent.interval_s = 1  # type: ignore[misc]


def test_dotenv_rejects_malformed_line(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\.env:2"):
        read_dotenv(write(tmp_path, "A=1\nnot a pair\n", ".env"))
