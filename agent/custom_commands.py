"""Discovery for custom commands (Slack-style workflow files).

Custom commands live as ``COMMAND.md`` files (YAML frontmatter + markdown body)
under ``~/.hermes/commands/<name>/``, plus any ``commands.external_dirs`` from
config. This mirrors the skill-loading machinery in :mod:`agent.skill_commands`
and reuses its frontmatter parser, directory walker, and platform gating.

The parsed schema and execution live in :mod:`agent.command_engine`; this module
only finds files on disk and turns them into a ``/command -> CommandSpec`` map.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from agent.command_engine import CommandConfigError, CommandSpec, parse_command
from agent.skill_utils import (
    iter_skill_index_files,
    parse_frontmatter,
    skill_matches_platform,
)

logger = logging.getLogger(__name__)

COMMAND_FILENAME = "COMMAND.md"

# Cache: command-key -> CommandSpec, plus the platform scope and commands dir
# it was built for (so the cache invalidates when either changes).
_command_cache: Dict[str, CommandSpec] = {}
_command_cache_platform: Optional[str] = None
_command_cache_home: Optional[str] = None


def get_commands_dir() -> Path:
    """Return ``~/.hermes/commands`` (under HERMES_HOME)."""
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "commands"


def get_external_command_dirs() -> List[Path]:
    """Read ``commands.external_dirs`` from config and return validated dirs.

    Mirrors :func:`agent.skill_utils.get_external_skills_dirs`: entries are
    ``~``/``${VAR}``-expanded, resolved against HERMES_HOME, de-duplicated, and
    filtered to existing directories.
    """
    from hermes_constants import get_config_path, get_hermes_home
    from agent.skill_utils import yaml_load

    config_path = get_config_path()
    if not config_path.exists():
        return []
    try:
        parsed = yaml_load(config_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(parsed, dict):
        return []
    cmd_cfg = parsed.get("commands")
    if not isinstance(cmd_cfg, dict):
        return []
    raw_dirs = cmd_cfg.get("external_dirs")
    if not raw_dirs:
        return []
    if isinstance(raw_dirs, str):
        raw_dirs = [raw_dirs]
    if not isinstance(raw_dirs, list):
        return []

    hermes_home = get_hermes_home()
    local = get_commands_dir().resolve()
    seen: set = set()
    result: List[Path] = []
    for entry in raw_dirs:
        entry = str(entry).strip()
        if not entry:
            continue
        expanded = os.path.expanduser(os.path.expandvars(entry))
        p = Path(expanded)
        p = (hermes_home / p).resolve() if not p.is_absolute() else p.resolve()
        if p == local or p in seen:
            continue
        if p.is_dir():
            seen.add(p)
            result.append(p)
    return result


def _get_disabled_command_names() -> set:
    """Read ``commands.disabled`` from config (parallel to skills.disabled)."""
    from hermes_constants import get_config_path
    from agent.skill_utils import yaml_load
    try:
        parsed = yaml_load(get_config_path().read_text(encoding="utf-8"))
        disabled = (parsed.get("commands") or {}).get("disabled") or []
        return {str(d).strip() for d in disabled if str(d).strip()}
    except Exception:
        return set()


def _resolve_platform_scope() -> Optional[str]:
    """Platform scope for cache invalidation (mirrors skill_commands)."""
    try:
        from gateway.session_context import get_session_env
        return (
            os.getenv("HERMES_PLATFORM")
            or get_session_env("HERMES_SESSION_PLATFORM")
            or None
        )
    except Exception:
        return os.getenv("HERMES_PLATFORM") or None


def scan_custom_commands() -> Dict[str, CommandSpec]:
    """Scan command dirs and return a ``/command -> CommandSpec`` mapping.

    Invalid files are logged and skipped — one bad command never breaks the rest.
    """
    global _command_cache, _command_cache_platform, _command_cache_home
    _command_cache_platform = _resolve_platform_scope()
    _command_cache_home = str(get_commands_dir())
    commands: Dict[str, CommandSpec] = {}
    disabled = _get_disabled_command_names()
    seen_names: set = set()

    dirs_to_scan: List[Path] = []
    local = get_commands_dir()
    if local.exists():
        dirs_to_scan.append(local)
    dirs_to_scan.extend(get_external_command_dirs())

    for scan_dir in dirs_to_scan:
        for cmd_md in iter_skill_index_files(scan_dir, COMMAND_FILENAME):
            try:
                content = cmd_md.read_text(encoding="utf-8")
                frontmatter, body = parse_frontmatter(content)
                if not skill_matches_platform(frontmatter):
                    continue
                spec = parse_command(frontmatter, body, source_path=str(cmd_md))
            except CommandConfigError as exc:
                logger.warning("Skipping invalid custom command %s: %s", cmd_md, exc)
                continue
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to load custom command %s: %s", cmd_md, exc)
                continue
            if spec.name in disabled or spec.command in disabled:
                continue
            if spec.command in seen_names:
                continue
            seen_names.add(spec.command)
            commands[f"/{spec.command}"] = spec

    _command_cache = commands
    return commands


def get_custom_commands() -> Dict[str, CommandSpec]:
    """Return the command map, scanning on first use or platform/home change."""
    if (
        not _command_cache
        or _command_cache_platform != _resolve_platform_scope()
        or _command_cache_home != str(get_commands_dir())
    ):
        scan_custom_commands()
    return _command_cache


def resolve_command_key(command: str) -> Optional[str]:
    """Resolve a typed ``/command`` to its canonical key (underscore↔hyphen)."""
    if not command:
        return None
    cmds = get_custom_commands()
    candidate = command.lstrip("/")
    for variant in (candidate, candidate.replace("_", "-"), candidate.replace("-", "_")):
        key = f"/{variant}"
        if key in cmds:
            return key
    return None


def get_command(command: str) -> Optional[CommandSpec]:
    """Resolve and return the CommandSpec for a typed ``/command``, or None."""
    key = resolve_command_key(command)
    return get_custom_commands().get(key) if key else None


def iter_command_entries() -> List[Tuple[str, str, str]]:
    """Yield ``(name, description, args_hint)`` for menu/help surfacing."""
    from agent.command_engine import args_hint
    out: List[Tuple[str, str, str]] = []
    for spec in get_custom_commands().values():
        out.append((spec.command, spec.description or f"Run /{spec.command}", args_hint(spec)))
    return out
