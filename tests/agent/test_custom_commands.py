"""Tests for custom-command discovery (agent.custom_commands)."""

import pytest

import agent.custom_commands as cc


CLAUDE_MD = """\
---
name: claude
description: Spawn remote-controlled Claude Code in a project
inputs:
  - name: session
    required: true
    description: Session name
steps:
  - id: pick
    form:
      project:
        type: choice
        description: Pick a project
        source:
          dirs: ~/code/git.home/*
          git_only: true
  - id: launch
    run:
      shell: cd {{pick.project}} && claude --worktree {{session}}
      detach: true
  - message: "launched {{pick.project}}"
---
Spawn a remote-controllable Claude Code session.
"""


@pytest.fixture
def cmd_env(tmp_path, monkeypatch):
    """Point HERMES_HOME at a tmp dir. The discovery cache is home-aware, so it
    invalidates automatically when HERMES_HOME changes between tests."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path, cc


def _write_command(home, name, content):
    d = home / "commands" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "COMMAND.md").write_text(content)


class TestDiscovery:
    def test_scan_finds_command(self, cmd_env):
        home, cc = cmd_env
        _write_command(home, "claude", CLAUDE_MD)
        cmds = cc.scan_custom_commands()
        assert "/claude" in cmds
        spec = cmds["/claude"]
        assert spec.command == "claude"
        assert spec.description.startswith("Spawn remote")
        assert spec.is_control_plane is True

    def test_resolve_underscore_hyphen(self, cmd_env):
        home, cc = cmd_env
        md = CLAUDE_MD.replace("name: claude", "name: my-cmd")
        _write_command(home, "my-cmd", md)
        cc.scan_custom_commands()
        assert cc.resolve_command_key("my-cmd") == "/my-cmd"
        assert cc.resolve_command_key("my_cmd") == "/my-cmd"  # Telegram underscore form
        assert cc.get_command("my_cmd").command == "my-cmd"

    def test_invalid_file_skipped(self, cmd_env):
        home, cc = cmd_env
        _write_command(home, "good", CLAUDE_MD)
        # Missing required `name` → invalid, must be skipped not crash.
        _write_command(home, "bad", "---\nsteps: []\n---\n")
        cmds = cc.scan_custom_commands()
        assert "/claude" in cmds
        assert len(cmds) == 1

    def test_disabled_excluded(self, cmd_env):
        home, cc = cmd_env
        _write_command(home, "claude", CLAUDE_MD)
        (home / "config.yaml").write_text("commands:\n  disabled: [claude]\n")
        cmds = cc.scan_custom_commands()
        assert "/claude" not in cmds

    def test_iter_command_entries(self, cmd_env):
        home, cc = cmd_env
        _write_command(home, "claude", CLAUDE_MD)
        cc.scan_custom_commands()
        entries = cc.iter_command_entries()
        assert ("claude", "Spawn remote-controlled Claude Code in a project", "<session>") in entries

    def test_no_commands_dir(self, cmd_env):
        home, cc = cmd_env
        assert cc.scan_custom_commands() == {}
        assert cc.get_command("claude") is None


class TestSurfacing:
    def test_telegram_and_help_include_custom(self, cmd_env):
        home, cc = cmd_env
        _write_command(home, "claude", CLAUDE_MD)
        cc.scan_custom_commands()

        from hermes_cli.commands import (
            _iter_custom_command_entries,
            gateway_help_lines,
            telegram_bot_commands,
        )
        entries = _iter_custom_command_entries()
        assert ("claude", "Spawn remote-controlled Claude Code in a project", "<session>") in entries
        # Telegram menu includes arg-taking custom commands (usage text on miss).
        assert any(name == "claude" for name, _desc in telegram_bot_commands())
        # /help renders a Custom commands section.
        help_text = "\n".join(gateway_help_lines())
        assert "Custom commands" in help_text
        assert "/claude" in help_text
