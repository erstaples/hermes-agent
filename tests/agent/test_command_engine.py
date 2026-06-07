"""Tests for the custom-command engine (agent.command_engine)."""

import asyncio

import pytest

from agent.command_engine import (
    Choice,
    ChoiceSource,
    CommandAbort,
    CommandConfigError,
    CommandUsageError,
    FormField,
    PromptHandoff,
    args_hint,
    bind_inputs,
    execute_steps,
    match_choice,
    parse_command,
    render_shell,
    render_text,
    resolve_choices,
)


# ── A fake frontend with scripted answers ────────────────────────────────

class FakeFrontend:
    def __init__(self, answers=None):
        self._answers = list(answers or [])
        self.messages = []
        self.collected = []

    async def collect(self, field, choices):
        self.collected.append((field.name, [c.label for c in choices]))
        return self._answers.pop(0) if self._answers else None

    async def notify(self, text):
        self.messages.append(text)


def _claude_spec(detach=True):
    return parse_command({
        "name": "claude",
        "description": "Spawn Claude Code",
        "inputs": [{"name": "session", "required": True}],
        "steps": [
            {"id": "pick", "form": {"project": {
                "type": "choice", "source": {"options": ["alpha", "beta"]}}}},
            {"id": "launch", "run": {
                "shell": "cd {{pick.project}} && claude --worktree {{session}}",
                "detach": detach}},
            {"message": "launched {{pick.project}} for {{session}}"},
        ],
    })


# ── Parsing / validation ──────────────────────────────────────────────────

class TestParse:
    def test_basic(self):
        spec = _claude_spec()
        assert spec.command == "claude"
        assert spec.is_control_plane is True
        assert spec.has_form_step is True
        assert args_hint(spec) == "<session>"

    def test_command_defaults_to_name(self):
        spec = parse_command({"name": "foo", "steps": [{"message": "hi"}]})
        assert spec.command == "foo"

    def test_explicit_command_override(self):
        spec = parse_command({"name": "foo", "command": "/bar", "steps": [{"message": "hi"}]})
        assert spec.command == "bar"

    def test_missing_name(self):
        with pytest.raises(CommandConfigError):
            parse_command({"steps": [{"message": "hi"}]})

    def test_empty_steps(self):
        with pytest.raises(CommandConfigError):
            parse_command({"name": "x", "steps": []})

    def test_unknown_variable_reference(self):
        with pytest.raises(CommandConfigError):
            parse_command({"name": "x", "steps": [{"run": "echo {{nope}}"}]})

    def test_choice_without_source(self):
        with pytest.raises(CommandConfigError):
            parse_command({"name": "x", "steps": [
                {"id": "f", "form": {"p": {"type": "choice"}}}]})

    def test_git_only_without_dirs(self):
        with pytest.raises(CommandConfigError):
            parse_command({"name": "x", "steps": [
                {"id": "f", "form": {"p": {"type": "choice",
                 "source": {"options": ["a"], "git_only": True}}}}]})

    def test_prompt_must_be_last(self):
        with pytest.raises(CommandConfigError):
            parse_command({"name": "x", "steps": [
                {"prompt": "do it"}, {"message": "after"}]})

    def test_duplicate_step_ids(self):
        with pytest.raises(CommandConfigError):
            parse_command({"name": "x", "steps": [
                {"id": "a", "message": "1"}, {"id": "a", "message": "2"}]})

    def test_source_multiple_kinds(self):
        with pytest.raises(CommandConfigError):
            parse_command({"name": "x", "steps": [
                {"id": "f", "form": {"p": {"type": "choice",
                 "source": {"options": ["a"], "dirs": "/x/*"}}}}]})

    def test_prompt_step_not_control_plane(self):
        spec = parse_command({"name": "x", "inputs": [{"name": "q", "required": True}],
                              "steps": [{"prompt": "Answer: {{q}}"}]})
        assert spec.is_control_plane is False
        assert spec.has_prompt_step is True


# ── Rendering / quoting ────────────────────────────────────────────────────

class TestRender:
    def test_shell_quotes_injection(self):
        out = render_shell("x {{a}}", {"a": "b; rm -rf /"})
        assert out == "x 'b; rm -rf /'"

    def test_shell_quotes_spaces(self):
        out = render_shell("cd {{p}}", {"p": "/x/y z"})
        assert out == "cd '/x/y z'"

    def test_nested_dotted_lookup(self):
        out = render_shell("{{s.field}}", {"s": {"field": "v"}})
        assert out == "v"

    def test_text_no_quoting(self):
        out = render_text("hi {{name}}", {"name": "a b"})
        assert out == "hi a b"

    def test_missing_ref_renders_empty(self):
        # parse-time validation guards real specs; the renderer itself is lenient
        assert render_text("x{{gone}}y", {}) == "xy"


# ── Choice resolution + matching ───────────────────────────────────────────

class TestChoices:
    def test_options(self):
        cs = ChoiceSource(options=["a", "b"])
        assert [c.value for c in resolve_choices(cs)] == ["a", "b"]

    def test_empty_aborts(self):
        cs = ChoiceSource(options=None, sh="true")  # sh that prints nothing
        with pytest.raises(CommandAbort):
            resolve_choices(cs)

    def test_dirs_filters_and_git_only(self, tmp_path):
        root = tmp_path / "projects"
        root.mkdir()
        (root / "repo").mkdir()
        (root / "repo" / ".git").mkdir()
        (root / "plain").mkdir()
        (root / "afile").write_text("x")
        cs = ChoiceSource(dirs=str(root / "*"), git_only=True)
        choices = resolve_choices(cs)
        assert [c.label for c in choices] == ["repo"]
        assert choices[0].value == str(root / "repo")

    def test_dirs_without_git_only(self, tmp_path):
        root = tmp_path / "projects"
        root.mkdir()
        (root / "a").mkdir()
        (root / "b").mkdir()
        (root / "f").write_text("x")
        cs = ChoiceSource(dirs=str(root / "*"))
        assert sorted(c.label for c in resolve_choices(cs)) == ["a", "b"]

    def test_sh_lines(self):
        cs = ChoiceSource(sh="printf 'one\\ntwo\\n'")
        assert [c.value for c in resolve_choices(cs)] == ["one", "two"]

    def test_match_exact_label(self):
        cs = [Choice("alpha", "A"), Choice("beta", "B")]
        assert match_choice("beta", cs).value == "B"

    def test_match_numeric_index(self):
        cs = [Choice("alpha", "A"), Choice("beta", "B")]
        assert match_choice("2", cs).value == "B"

    def test_match_case_insensitive(self):
        cs = [Choice("Alpha", "A")]
        assert match_choice("alpha", cs).value == "A"

    def test_match_no_match(self):
        cs = [Choice("a", "A")]
        assert match_choice("zzz", cs) is None


# ── Input binding ──────────────────────────────────────────────────────────

class TestBindInputs:
    def test_positional(self):
        spec = _claude_spec()
        assert bind_inputs(spec, "mysession") == {"session": "mysession"}

    def test_missing_required_raises_usage(self):
        spec = _claude_spec()
        with pytest.raises(CommandUsageError):
            bind_inputs(spec, "")

    def test_default_used(self):
        spec = parse_command({"name": "x", "inputs": [
            {"name": "a", "default": "dflt"}], "steps": [{"message": "{{a}}"}]})
        assert bind_inputs(spec, "") == {"a": "dflt"}

    def test_unbalanced_quotes_raises_usage(self):
        spec = _claude_spec()
        with pytest.raises(CommandUsageError):
            bind_inputs(spec, 'a "unterminated')


# ── Execution ──────────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.run(coro)


class TestExecute:
    def test_multi_step_variable_flow_detach(self, monkeypatch):
        spawned = {}

        def fake_spawn(command, env=None):
            spawned["command"] = command
            spawned["env"] = env
            return 4242

        monkeypatch.setattr("agent.command_engine.spawn_detached", fake_spawn)
        spec = _claude_spec(detach=True)
        ctx = bind_inputs(spec, "sess1")
        fe = FakeFrontend(answers=["beta"])
        handoff = _run(execute_steps(spec, ctx, fe))
        assert handoff is None
        # {{pick.project}} flowed into the launch step; values shell-quoted.
        assert spawned["command"] == "cd beta && claude --worktree sess1"
        assert spawned["env"] is None  # detach inherits full env
        assert ctx["launch"]["pid"] == "4242"
        assert fe.messages == ["launched beta for sess1"]

    def test_single_option_autoselects(self, monkeypatch):
        monkeypatch.setattr("agent.command_engine.spawn_detached", lambda c, env=None: 1)
        spec = parse_command({
            "name": "x", "inputs": [{"name": "s", "required": True}],
            "steps": [
                {"id": "pick", "form": {"p": {"type": "choice",
                 "source": {"options": ["only"]}}}},
                {"id": "go", "run": {"shell": "echo {{pick.p}}", "detach": True}},
            ],
        })
        ctx = bind_inputs(spec, "v")
        fe = FakeFrontend(answers=[])  # collect must NOT be called
        _run(execute_steps(spec, ctx, fe))
        assert fe.collected == []
        assert ctx["pick"]["p"] == "only"

    def test_capture_step_records_stdout(self):
        spec = parse_command({"name": "x", "steps": [
            {"id": "r", "run": {"shell": "printf hello"}},
            {"message": "got {{r.stdout}}"}]})
        ctx = {}
        fe = FakeFrontend()
        _run(execute_steps(spec, ctx, fe))
        assert ctx["r"]["stdout"] == "hello"
        assert fe.messages == ["got hello"]

    def test_no_match_aborts(self, monkeypatch):
        spec = _claude_spec()
        ctx = bind_inputs(spec, "sess")
        fe = FakeFrontend(answers=["nonexistent"])
        with pytest.raises(CommandAbort):
            _run(execute_steps(spec, ctx, fe))

    def test_cancel_aborts(self):
        spec = _claude_spec()
        ctx = bind_inputs(spec, "sess")
        fe = FakeFrontend(answers=[None])  # timeout / cancel
        with pytest.raises(CommandAbort):
            _run(execute_steps(spec, ctx, fe))

    def test_prompt_handoff(self):
        spec = parse_command({"name": "x", "inputs": [{"name": "q", "required": True}],
                              "steps": [{"prompt": "Please answer: {{q}}"}]})
        ctx = bind_inputs(spec, "why")
        fe = FakeFrontend()
        handoff = _run(execute_steps(spec, ctx, fe))
        assert isinstance(handoff, PromptHandoff)
        assert handoff.text == "Please answer: why"

    def test_redact_applied_to_capture(self):
        spec = parse_command({"name": "x", "steps": [
            {"id": "r", "run": {"shell": "printf secret"}},
            {"message": "{{r.stdout}}"}]})
        ctx = {}
        fe = FakeFrontend()
        _run(execute_steps(spec, ctx, fe, redact=lambda s: s.replace("secret", "[redacted]")))
        assert ctx["r"]["stdout"] == "[redacted]"
