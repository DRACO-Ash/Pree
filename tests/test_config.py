"""Configuration is the boot-time fail-closed boundary, so every rejection has a test.

Paths come from the tmp_path fixture rather than a fixed temporary directory, so no test can
collide with another process or leave state behind.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pree.config import DEFAULT_PORT, MAX_PORT, MIN_PORT, ConfigError, load_config


def test_port_defaults_to_8080_when_unset(tmp_path: Path) -> None:
    assert load_config({"PREE_DATA_DIR": str(tmp_path)}).port == DEFAULT_PORT


def test_port_is_read_from_the_platform_injected_variable(tmp_path: Path) -> None:
    config = load_config({"PORT": "3000", "PREE_DATA_DIR": str(tmp_path)})
    assert config.port == 3000


@pytest.mark.parametrize("bad", ["0", "70000", "eight", "-1", "80.5"])
def test_unusable_port_is_rejected(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ConfigError, match="PORT"):
        load_config({"PORT": bad, "PREE_DATA_DIR": str(tmp_path)})


def test_the_port_bounds_are_the_full_valid_range(tmp_path: Path) -> None:
    for edge in (MIN_PORT, MAX_PORT):
        assert load_config({"PORT": str(edge), "PREE_DATA_DIR": str(tmp_path)}).port == edge


def test_blank_values_count_as_absent_not_as_configured(tmp_path: Path) -> None:
    config = load_config({"PREE_TEAM_TOKEN": "   ", "PREE_DATA_DIR": str(tmp_path)})
    assert config.team_token is None
    assert config.auth_enabled is False


def test_data_dir_prefers_explicit_then_injected_then_default(tmp_path: Path) -> None:
    injected = str(tmp_path / "injected")
    explicit = str(tmp_path / "explicit")
    both = load_config({"PREE_DATA_DIR": explicit, "STORAGE_MOUNT_PATH": injected})
    assert both.data_dir == Path(explicit)
    assert load_config({"STORAGE_MOUNT_PATH": injected}).data_dir == Path(injected)
    assert load_config({}).data_dir == Path("./data").resolve()


def test_filesystem_root_as_data_dir_is_rejected() -> None:
    with pytest.raises(ConfigError, match="filesystem root"):
        load_config({"PREE_DATA_DIR": "/"})


def test_token_without_allowed_origin_refuses_to_start_in_production(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no PREE_ALLOWED_ORIGIN"):
        load_config(
            {
                "PREE_ENV": "production",
                "PREE_TEAM_TOKEN": "t",
                "PREE_DATA_DIR": str(tmp_path),
            }
        )


def test_wildcard_origin_with_a_token_refuses_to_start_in_production(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"is '\*' with a token set"):
        load_config(
            {
                "PREE_ENV": "production",
                "PREE_TEAM_TOKEN": "t",
                "PREE_ALLOWED_ORIGIN": "*",
                "PREE_DATA_DIR": str(tmp_path),
            }
        )


def test_a_named_origin_with_a_token_starts_in_production(tmp_path: Path) -> None:
    config = load_config(
        {
            "PREE_ENV": "production",
            "PREE_TEAM_TOKEN": "t",
            "PREE_ALLOWED_ORIGIN": "https://pree.apps.bluestaq.com",
            "PREE_DATA_DIR": str(tmp_path),
        }
    )
    assert config.is_production is True
    assert config.auth_enabled is True


def test_development_mode_tolerates_a_token_without_an_origin(tmp_path: Path) -> None:
    config = load_config({"PREE_TEAM_TOKEN": "t", "PREE_DATA_DIR": str(tmp_path)})
    assert config.auth_enabled is True
    assert config.is_production is False


def test_unknown_environment_name_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="PREE_ENV"):
        load_config({"PREE_ENV": "staging", "PREE_DATA_DIR": str(tmp_path)})


def test_build_id_falls_back_to_unknown_rather_than_a_guess(tmp_path: Path) -> None:
    assert load_config({"PREE_DATA_DIR": str(tmp_path)}).build_id == "unknown"


def test_production_refuses_to_start_with_no_token_at_all(tmp_path: Path) -> None:
    """The absent token was the posture originally missed.

    The pairing check returned early whenever the token was None, so it caught a token with a
    bad origin and waved through no token at all. That left production serving read and write
    of the assessment store to any caller reaching the ingress.
    """
    with pytest.raises(ConfigError, match="no PREE_TEAM_TOKEN"):
        load_config({"PREE_ENV": "production", "PREE_DATA_DIR": str(tmp_path)})


def test_production_refuses_a_wildcard_origin_even_with_no_token(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no PREE_TEAM_TOKEN"):
        load_config(
            {
                "PREE_ENV": "production",
                "PREE_ALLOWED_ORIGIN": "*",
                "PREE_DATA_DIR": str(tmp_path),
            }
        )


def test_development_is_the_only_place_the_open_mode_is_reachable(tmp_path: Path) -> None:
    config = load_config({"PREE_DATA_DIR": str(tmp_path)})
    assert config.auth_enabled is False
    assert config.is_production is False
