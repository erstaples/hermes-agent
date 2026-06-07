"""Gateway-side dispatch tests for custom commands.

Exercises GatewayRunner._handle_custom_command and the nested gateway frontend
against a lightweight fake `self` so we don't stand up a full runner.
"""

import asyncio
import types

import pytest

from agent.command_engine import parse_command
from gateway.run import GatewayRunner


def _spec(detach=True, with_form=True, prompt=False):
    steps = []
    if with_form:
        steps.append({"id": "pick", "form": {"project": {
            "type": "choice", "source": {"options": ["alpha", "beta"]}}}})
    if prompt:
        steps.append({"prompt": "Answer for {{session}}"})
    else:
        ref = "{{pick.project}}" if with_form else "{{session}}"
        steps.append({"id": "launch", "run": {
            "shell": f"echo started {ref} {{{{session}}}}", "detach": detach}})
        steps.append({"message": f"launched {ref}"})
    return parse_command({
        "name": "claude",
        "description": "Spawn Claude Code",
        "inputs": [{"name": "session", "required": True}],
        "steps": steps,
    })


class FakeSendResult:
    success = True


class FakeAdapter:
    def __init__(self):
        self.sent = []
        self.clarify_calls = []

    async def send(self, chat_id, content, metadata=None):
        self.sent.append(content)
        return FakeSendResult()

    async def send_clarify(self, chat_id, question, choices, clarify_id,
                           session_key, metadata=None):
        self.clarify_calls.append((question, choices, clarify_id))
        # Resolve immediately as if the user clicked a button.
        from tools.clarify_gateway import resolve_gateway_clarify
        resolve_gateway_clarify(clarify_id, "beta")
        return FakeSendResult()


class FakeSource:
    platform = "telegram"
    chat_id = "chat1"
    user_id = "user1"


def _make_event(args="sess1"):
    ev = types.SimpleNamespace()
    ev.source = FakeSource()
    ev.message_id = "m1"
    ev.text = f"/claude {args}"
    ev.get_command_args = lambda: args
    return ev


def _fake_self(adapter):
    s = types.SimpleNamespace()
    s.adapters = {FakeSource.platform: adapter}
    s._background_tasks = set()
    s._session_key_for_source = lambda src: "telegram:chat1"
    s._thread_metadata_for_source = lambda src, mid: None
    s._check_slash_access = lambda src, cmd: None
    # Bind the real methods/classes onto the fake.
    s._handle_custom_command = GatewayRunner._handle_custom_command.__get__(s)
    s._GatewayCommandFrontend = GatewayRunner._GatewayCommandFrontend
    return s


def _drain(fake_self):
    """Run any background tasks the handler scheduled to completion."""
    tasks = list(fake_self._background_tasks)
    if tasks:
        asyncio.get_event_loop().run_until_complete(asyncio.gather(*tasks))


class TestGatewayDispatch:
    def test_usage_error_returned_sync(self):
        adapter = FakeAdapter()
        fs = _fake_self(adapter)
        ev = _make_event(args="")  # missing required input
        result = asyncio.run(fs._handle_custom_command(ev, _spec()))
        assert result.startswith("Usage: /claude")

    def test_control_plane_runs_in_background_with_picker(self, monkeypatch):
        spawned = {}
        monkeypatch.setattr(
            "agent.command_engine.spawn_detached",
            lambda command, env=None: spawned.setdefault("command", command) or 999,
        )

        async def scenario():
            adapter = FakeAdapter()
            fs = _fake_self(adapter)
            ev = _make_event("sess1")
            result = await fs._handle_custom_command(ev, _spec(detach=True))
            assert result == ""  # async: follow-up arrives later
            await asyncio.gather(*fs._background_tasks)
            return adapter

        adapter = asyncio.run(scenario())
        # Picker was rendered via send_clarify; user "beta" flowed into the shell.
        assert adapter.clarify_calls
        assert spawned["command"] == "echo started beta sess1"
        assert adapter.sent == ["launched beta"]

    def test_text_fallback_when_no_send_clarify(self, monkeypatch):
        spawned = {}
        monkeypatch.setattr(
            "agent.command_engine.spawn_detached",
            lambda command, env=None: spawned.setdefault("command", command) or 1,
        )

        async def scenario():
            adapter = FakeAdapter()
            # No send_clarify → the fallback numbered-list path runs.
            adapter.send_clarify = None

            # A monkeypatched wait_for_response simulates the user replying "2".
            import tools.clarify_gateway as cg
            monkeypatch.setattr(cg, "wait_for_response", lambda cid, t: "2")

            fs = _fake_self(adapter)
            ev = _make_event("sess1")
            result = await fs._handle_custom_command(ev, _spec(detach=True))
            assert result == ""
            await asyncio.gather(*fs._background_tasks)
            return adapter

        adapter = asyncio.run(scenario())
        # Numbered menu was sent, then the launch message.
        assert any("1. alpha" in s for s in adapter.sent)
        assert spawned["command"] == "echo started beta sess1"

    def test_prompt_command_without_form_falls_through(self):
        adapter = FakeAdapter()
        fs = _fake_self(adapter)
        ev = _make_event("why")
        spec = _spec(with_form=False, prompt=True)
        result = asyncio.run(fs._handle_custom_command(ev, spec))
        assert result is None  # signals fall-through to agent
        assert ev.text == "Answer for why"

    def test_prompt_command_with_form_rejected(self):
        adapter = FakeAdapter()
        fs = _fake_self(adapter)
        ev = _make_event("why")
        spec = _spec(with_form=True, prompt=True)
        result = asyncio.run(fs._handle_custom_command(ev, spec))
        assert "aren't supported" in result
