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


# --- 1.2 [heartbeat] block + reserved 'system' name ---------------------

def test_heartbeat_defaults_when_section_missing(tmp_path: Path) -> None:
    p = _write(tmp_path, "")
    cfg = load_config(p)
    assert cfg.heartbeat.online_seconds == 90
    assert cfg.heartbeat.stale_seconds == 300


def test_heartbeat_explicit_values(tmp_path: Path) -> None:
    p = _write(tmp_path, """
        [heartbeat]
        online_seconds = 30
        stale_seconds = 120
    """)
    cfg = load_config(p)
    assert cfg.heartbeat.online_seconds == 30
    assert cfg.heartbeat.stale_seconds == 120


def test_heartbeat_online_must_be_less_than_stale(tmp_path: Path) -> None:
    p = _write(tmp_path, """
        [heartbeat]
        online_seconds = 200
        stale_seconds = 100
    """)
    with pytest.raises(ConfigError, match="online_seconds"):
        load_config(p)


def test_heartbeat_negative_value_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, """
        [heartbeat]
        online_seconds = -1
    """)
    with pytest.raises(ConfigError, match="positive"):
        load_config(p)


def test_heartbeat_wrong_type_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, """
        [heartbeat]
        online_seconds = "ninety"
    """)
    with pytest.raises(ConfigError, match="int"):
        load_config(p)


def test_reserved_name_system_rejected(tmp_path: Path) -> None:
    """'system' is reserved as the agent_stop sender — can't be a participant."""
    p = _write(tmp_path, """
        [participants.system]
        role    = "human"
        display = "System"
    """)
    with pytest.raises(ConfigError, match="reserved"):
        load_config(p)


# --- 1.2.2 [system] block + bright_black color ---------------------------

def test_system_color_default_when_section_missing(tmp_path: Path) -> None:
    p = _write(tmp_path, "")
    cfg = load_config(p)
    assert cfg.system.color == "bright_black"


def test_system_color_explicit(tmp_path: Path) -> None:
    p = _write(tmp_path, """
        [system]
        color = "yellow"
    """)
    cfg = load_config(p)
    assert cfg.system.color == "yellow"


def test_system_color_unknown_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, """
        [system]
        color = "chartreuse"
    """)
    with pytest.raises(ConfigError, match="color"):
        load_config(p)


def test_bright_black_now_allowed(tmp_path: Path) -> None:
    """1.2.2 added bright_black to the allowed color set."""
    p = _write(tmp_path, """
        [participants.alice]
        role    = "human"
        display = "Alice"
        color   = "bright_black"
    """)
    cfg = load_config(p)
    assert cfg.participants["alice"].color == "bright_black"
