from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ALLOWED_ROLES: frozenset[str] = frozenset({
    "human", "claude_desktop", "claude_code", "other",
})

ALLOWED_COLORS: frozenset[str] = frozenset({
    "white", "cyan", "magenta", "green", "yellow", "blue", "red",
    "bright_white", "bright_cyan", "bright_magenta", "bright_green",
    "bright_yellow", "bright_blue", "bright_red",
})

RESERVED_NAMES: frozenset[str] = frozenset({"all"})

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


class ConfigError(ValueError):
    """Raised when terminal_share.toml fails validation."""


@dataclass(frozen=True)
class Server:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT


@dataclass(frozen=True)
class Participant:
    name: str
    role: str
    display: str
    color: str = "white"


@dataclass(frozen=True)
class Config:
    server: Server
    participants: dict[str, Participant] = field(default_factory=dict)


def load_config(path: str | Path) -> Config:
    p = Path(path)
    with p.open("rb") as f:
        data = tomllib.load(f)
    return _parse(data)


def _parse(data: dict[str, Any]) -> Config:
    server = _parse_server(data.get("server", {}))
    participants = _parse_participants(data.get("participants", {}))
    return Config(server=server, participants=participants)


def _parse_server(raw: Any) -> Server:
    if not isinstance(raw, dict):
        raise ConfigError("[server] must be a table")
    host = raw.get("host", DEFAULT_HOST)
    port = raw.get("port", DEFAULT_PORT)
    if not isinstance(host, str):
        raise ConfigError(f"[server].host must be a string, got {type(host).__name__}")
    if isinstance(port, bool) or not isinstance(port, int):
        raise ConfigError(f"[server].port must be an int, got {type(port).__name__}")
    if not 1 <= port <= 65535:
        raise ConfigError(f"[server].port {port} out of range 1..65535")
    return Server(host=host, port=port)


def _parse_participants(raw: Any) -> dict[str, Participant]:
    if not isinstance(raw, dict):
        raise ConfigError("[participants] must be a table")
    out: dict[str, Participant] = {}
    for name, body in raw.items():
        if name.lower() in RESERVED_NAMES:
            raise ConfigError(
                f"participant name '{name}' is reserved (collides with @all broadcast)"
            )
        if not isinstance(body, dict):
            raise ConfigError(f"[participants.{name}] must be a table")
        role = body.get("role")
        if role is None:
            raise ConfigError(f"[participants.{name}] is missing required 'role'")
        if role not in ALLOWED_ROLES:
            raise ConfigError(
                f"[participants.{name}].role '{role}' not in {sorted(ALLOWED_ROLES)}"
            )
        display = body.get("display")
        if not isinstance(display, str) or not display.strip():
            raise ConfigError(
                f"[participants.{name}] is missing or has empty 'display'"
            )
        color = body.get("color", "white")
        if color not in ALLOWED_COLORS:
            raise ConfigError(
                f"[participants.{name}].color '{color}' not in {sorted(ALLOWED_COLORS)}"
            )
        out[name] = Participant(name=name, role=role, display=display, color=color)
    if out:
        humans = [p for p in out.values() if p.role == "human"]
        if len(humans) != 1:
            raise ConfigError(
                f"exactly one participant must have role='human' (found {len(humans)})"
            )
    return out
