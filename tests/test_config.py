from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from terminal_share.config import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    ConfigError,
    load_config,
)


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "terminal_share.toml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def test_empty_file_uses_defaults(tmp_path: Path) -> None:
    p = _write(tmp_path, "")
    cfg = load_config(p)
    assert cfg.server.host == DEFAULT_HOST
    assert cfg.server.port == DEFAULT_PORT
    assert cfg.participants == {}


def test_explicit_server_block(tmp_path: Path) -> None:
    p = _write(tmp_path, """
        [server]
        host = "0.0.0.0"
        port = 8800
    """)
    cfg = load_config(p)
    assert cfg.server.host == "0.0.0.0"
    assert cfg.server.port == 8800


def test_port_out_of_range_high(tmp_path: Path) -> None:
    p = _write(tmp_path, "[server]\nport = 70000\n")
    with pytest.raises(ConfigError, match="port"):
        load_config(p)


def test_port_out_of_range_low(tmp_path: Path) -> None:
    p = _write(tmp_path, "[server]\nport = 0\n")
    with pytest.raises(ConfigError, match="port"):
        load_config(p)


def test_port_wrong_type(tmp_path: Path) -> None:
    p = _write(tmp_path, '[server]\nport = "8765"\n')
    with pytest.raises(ConfigError, match="int"):
        load_config(p)


def test_reserved_name_all_lowercase(tmp_path: Path) -> None:
    p = _write(tmp_path, """
        [participants.all]
        role    = "human"
        display = "All"
    """)
    with pytest.raises(ConfigError, match="reserved"):
        load_config(p)


def test_reserved_name_all_uppercase(tmp_path: Path) -> None:
    p = _write(tmp_path, """
        [participants.ALL]
        role    = "human"
        display = "All"
    """)
    with pytest.raises(ConfigError, match="reserved"):
        load_config(p)


def test_unknown_role_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, """
        [participants.bob]
        role    = "robot"
        display = "Bob"
    """)
    with pytest.raises(ConfigError, match="role"):
        load_config(p)


def test_unknown_color_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, """
        [participants.bob]
        role    = "human"
        display = "Bob"
        color   = "chartreuse"
    """)
    with pytest.raises(ConfigError, match="color"):
        load_config(p)


def test_two_humans_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, """
        [participants.alice]
        role    = "human"
        display = "Alice"

        [participants.bob]
        role    = "human"
        display = "Bob"
    """)
    with pytest.raises(ConfigError, match="human"):
        load_config(p)


def test_zero_humans_rejected_when_participants_present(tmp_path: Path) -> None:
    p = _write(tmp_path, """
        [participants.claudia]
        role    = "claude_desktop"
        display = "Claudia"
    """)
    with pytest.raises(ConfigError, match="human"):
        load_config(p)


def test_missing_role_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, """
        [participants.bob]
        display = "Bob"
    """)
    with pytest.raises(ConfigError, match="role"):
        load_config(p)


def test_missing_display_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, """
        [participants.bob]
        role = "human"
    """)
    with pytest.raises(ConfigError, match="display"):
        load_config(p)


def test_empty_display_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, """
        [participants.bob]
        role    = "human"
        display = "   "
    """)
    with pytest.raises(ConfigError, match="display"):
        load_config(p)


def test_full_example_loads(tmp_path: Path) -> None:
    p = _write(tmp_path, """
        [server]
        host = "127.0.0.1"
        port = 8765

        [participants.serinety]
        role    = "human"
        display = "Serinety"
        color   = "cyan"

        [participants.claudia]
        role    = "claude_desktop"
        display = "Claudia"
        color   = "magenta"

        [participants.code]
        role    = "claude_code"
        display = "Claude Code"
        color   = "green"
    """)
    cfg = load_config(p)
    assert cfg.server.port == 8765
    assert len(cfg.participants) == 3
    assert cfg.participants["serinety"].role == "human"
    assert cfg.participants["claudia"].color == "magenta"
    assert cfg.participants["code"].display == "Claude Code"


def test_color_defaults_to_white(tmp_path: Path) -> None:
    p = _write(tmp_path, """
        [participants.bob]
        role    = "human"
        display = "Bob"
    """)
    cfg = load_config(p)
    assert cfg.participants["bob"].color == "white"


def test_shipped_example_file_loads() -> None:
    """The terminal_share.toml at the repo root must be valid."""
    here = Path(__file__).resolve().parent.parent
    cfg = load_config(here / "terminal_share.toml")
    assert cfg.server.port == 8765
    assert "serinety" in cfg.participants
