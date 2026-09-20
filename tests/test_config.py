"""Tests for environment and TOML configuration resolution.

Every test passes an explicit environment mapping and uses ``tmp_path`` for
TOML files, so nothing depends on the machine's real environment or on the
repository working directory.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from orca_presence.config import (
    CLIENT_ID_ENV,
    CONFIG_FILE_ENV,
    DEFAULT_CONFIG_FILE_NAME,
    DEFAULT_ORCA_EXECUTABLE,
    DEFAULT_ORCA_TIMEOUT_SECONDS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    ORCA_EXECUTABLE_ENV,
    ORCA_TIMEOUT_ENV,
    POLL_INTERVAL_ENV,
    Config,
    ConfigError,
    find_config_file,
)

VALID_CLIENT_ID = "123456789012345678"
OTHER_CLIENT_ID = "999999999999999999"
FULL_TOML = """\
client_id = "123456789012345678"
poll_interval_seconds = 30.0
orca_executable = "C:/tools/orca.exe"
orca_timeout_seconds = 2.5
"""


def _write_config(
    tmp_path: Path, content: str, name: str = DEFAULT_CONFIG_FILE_NAME
) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_documented_defaults() -> None:
    assert DEFAULT_POLL_INTERVAL_SECONDS == 15.0
    assert DEFAULT_ORCA_EXECUTABLE == "orca"
    assert DEFAULT_ORCA_TIMEOUT_SECONDS == 5.0
    assert DEFAULT_CONFIG_FILE_NAME == "orca-presence.toml"


def test_load_uses_documented_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)

    config = Config.load(env={CLIENT_ID_ENV: VALID_CLIENT_ID})

    assert config.client_id == VALID_CLIENT_ID
    assert config.poll_interval_seconds == DEFAULT_POLL_INTERVAL_SECONDS
    assert config.orca_executable == DEFAULT_ORCA_EXECUTABLE
    assert config.orca_timeout_seconds == DEFAULT_ORCA_TIMEOUT_SECONDS


def test_client_id_is_stripped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)

    config = Config.load(env={CLIENT_ID_ENV: f"  {VALID_CLIENT_ID}  "})

    assert config.client_id == VALID_CLIENT_ID


def test_missing_client_id_is_a_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigError, match=CLIENT_ID_ENV):
        Config.load(env={})


def test_blank_client_id_is_a_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigError):
        Config.load(env={CLIENT_ID_ENV: "   "})


def test_toml_file_provides_all_values(tmp_path: Path) -> None:
    path = _write_config(tmp_path, FULL_TOML)

    config = Config.load(path, env={})

    assert config.client_id == VALID_CLIENT_ID
    assert config.poll_interval_seconds == 30.0
    assert config.orca_executable == "C:/tools/orca.exe"
    assert config.orca_timeout_seconds == 2.5


def test_environment_overrides_toml(tmp_path: Path) -> None:
    path = _write_config(tmp_path, FULL_TOML)

    config = Config.load(
        path,
        env={
            CLIENT_ID_ENV: OTHER_CLIENT_ID,
            POLL_INTERVAL_ENV: "5",
            ORCA_EXECUTABLE_ENV: "orca-dev",
            ORCA_TIMEOUT_ENV: "1.5",
        },
    )

    assert config.client_id == OTHER_CLIENT_ID
    assert config.poll_interval_seconds == 5.0
    assert config.orca_executable == "orca-dev"
    assert config.orca_timeout_seconds == 1.5


def test_explicit_path_wins_over_the_environment(tmp_path: Path) -> None:
    env_path = _write_config(
        tmp_path, f'client_id = "{OTHER_CLIENT_ID}"\n', name="env.toml"
    )
    arg_path = _write_config(
        tmp_path, f'client_id = "{VALID_CLIENT_ID}"\n', name="arg.toml"
    )

    config = Config.load(arg_path, env={CONFIG_FILE_ENV: str(env_path)})

    assert config.client_id == VALID_CLIENT_ID


def test_environment_selected_config_file_is_used(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path, f'client_id = "{OTHER_CLIENT_ID}"\n', name="env.toml"
    )

    config = Config.load(env={CONFIG_FILE_ENV: str(path)})

    assert config.client_id == OTHER_CLIENT_ID


def test_default_config_file_in_cwd_is_used(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path, f'client_id = "{VALID_CLIENT_ID}"\n')

    config = Config.load(env={})

    assert config.client_id == VALID_CLIENT_ID


def test_missing_explicit_config_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="path argument"):
        Config.load(tmp_path / "missing.toml", env={})


def test_missing_environment_config_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=CONFIG_FILE_ENV):
        Config.load(env={CONFIG_FILE_ENV: str(tmp_path / "missing.toml")})


def test_blank_environment_config_file_is_an_error() -> None:
    with pytest.raises(ConfigError, match=CONFIG_FILE_ENV):
        Config.load(env={CONFIG_FILE_ENV: "   "})


def test_unknown_toml_key_is_rejected(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path, f'client_id = "{VALID_CLIENT_ID}"\npoll_interval = 5\n'
    )

    with pytest.raises(ConfigError, match="poll_interval"):
        Config.load(path, env={})


def test_invalid_toml_is_rejected(tmp_path: Path) -> None:
    path = _write_config(tmp_path, "client_id = [unterminated\n")

    with pytest.raises(ConfigError, match="TOML"):
        Config.load(path, env={})


def test_non_string_client_id_in_toml_is_rejected(tmp_path: Path) -> None:
    path = _write_config(tmp_path, "client_id = 123456789012345678\n")

    with pytest.raises(ConfigError, match="string"):
        Config.load(path, env={})


def test_boolean_toml_number_is_rejected(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path, f'client_id = "{VALID_CLIENT_ID}"\npoll_interval_seconds = true\n'
    )

    with pytest.raises(ConfigError, match="number"):
        Config.load(path, env={})


@pytest.mark.parametrize("value", ["0", "-1", "abc", "nan", "inf", "", "  "])
def test_invalid_poll_interval_environment_value_is_rejected(
    value: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigError):
        Config.load(env={CLIENT_ID_ENV: VALID_CLIENT_ID, POLL_INTERVAL_ENV: value})


def test_blank_executable_environment_value_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigError, match=ORCA_EXECUTABLE_ENV):
        Config.load(env={CLIENT_ID_ENV: VALID_CLIENT_ID, ORCA_EXECUTABLE_ENV: "  "})


def test_direct_construction_validates_and_normalizes() -> None:
    config = Config(VALID_CLIENT_ID, poll_interval_seconds=20, orca_timeout_seconds=3)

    assert config.poll_interval_seconds == 20.0
    assert isinstance(config.poll_interval_seconds, float)
    assert config.orca_timeout_seconds == 3.0


def test_direct_construction_expands_the_home_directory() -> None:
    config = Config(VALID_CLIENT_ID, orca_executable="~/bin/orca")

    assert config.orca_executable == str(Path("~/bin/orca").expanduser())


@pytest.mark.parametrize(
    ("client_id", "poll_interval", "timeout", "executable"),
    [
        ("", 15.0, 5.0, "orca"),
        ("   ", 15.0, 5.0, "orca"),
        (VALID_CLIENT_ID, 0.0, 5.0, "orca"),
        (VALID_CLIENT_ID, -1.0, 5.0, "orca"),
        (VALID_CLIENT_ID, float("nan"), 5.0, "orca"),
        (VALID_CLIENT_ID, float("inf"), 5.0, "orca"),
        (VALID_CLIENT_ID, 15.0, 0.0, "orca"),
        (VALID_CLIENT_ID, 15.0, 5.0, "  "),
    ],
)
def test_direct_construction_rejects_invalid_values(
    client_id: str, poll_interval: float, timeout: float, executable: str
) -> None:
    with pytest.raises(ConfigError):
        Config(
            client_id,
            poll_interval_seconds=poll_interval,
            orca_timeout_seconds=timeout,
            orca_executable=executable,
        )


def test_config_is_frozen() -> None:
    config = Config(VALID_CLIENT_ID)

    with pytest.raises(dataclasses.FrozenInstanceError):
        config.__setattr__("client_id", OTHER_CLIENT_ID)


def test_equal_inputs_produce_equal_configs(tmp_path: Path) -> None:
    path = _write_config(tmp_path, FULL_TOML)

    first = Config.load(path, env={})
    second = Config.load(path, env={})

    assert first == second


def test_find_config_file_returns_none_without_a_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)

    assert find_config_file(env={}) is None


def test_find_config_file_returns_the_default_when_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_config(tmp_path, f'client_id = "{VALID_CLIENT_ID}"\n')

    assert find_config_file(env={}) == Path(DEFAULT_CONFIG_FILE_NAME)
    assert (tmp_path / DEFAULT_CONFIG_FILE_NAME).is_file()
