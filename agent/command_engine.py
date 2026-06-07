"""Custom-command engine — Slack-style workflows for hermes-agent.

A *custom command* is a user-authored workflow file (``COMMAND.md`` with YAML
frontmatter, see :mod:`agent.custom_commands` for discovery). It decomposes into
three Slack-Workflow-Builder-style parts:

  * **Trigger** — the slash command plus how message args bind to *inputs*.
  * **Inputs collected via forms** — typed fields; any value not already bound
    from the trigger is collected interactively. A "picker" is not a feature:
    it is a ``choice`` field whose value wasn't supplied, so a form asks for it.
  * **Steps + variables** — ordered actions referencing earlier values with
    ``{{var}}`` interpolation.

This module is the frontend-agnostic core: it parses/validates a spec, resolves
choice sources, renders templates (shell-quoting interpolated values), and runs
the steps against a :class:`CommandFrontend` that each surface (CLI / gateway)
implements. Keeping interaction behind the protocol makes the engine pure and
unit-testable with a fake frontend.
"""

from __future__ import annotations

import asyncio
import glob
import logging
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Union, runtime_checkable

logger = logging.getLogger(__name__)

# Matches {{ name }} or {{ step.field }} — alnum, underscore, dot, hyphen.
_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.\-]+)\s*\}\}")

# Field/input types supported in v1.
_VALID_TYPES = frozenset({"text", "choice"})

DEFAULT_RUN_TIMEOUT = 30


# =========================================================================
# Errors / control-flow signals
# =========================================================================

class CommandConfigError(ValueError):
    """A command file is malformed (raised at parse time)."""


class CommandUsageError(Exception):
    """Required inputs weren't supplied — carries user-facing usage text."""

    def __init__(self, usage: str):
        self.usage = usage
        super().__init__(usage)


class CommandAbort(Exception):
    """A step failed in a way that should stop the workflow with a message."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


@dataclass
class PromptHandoff:
    """Terminal result: inject ``text`` into the agent as the turn's message."""
    text: str


# =========================================================================
# Schema dataclasses
# =========================================================================

@dataclass(frozen=True)
class Choice:
    """One selectable option. ``label`` is shown/matched; ``value`` substituted."""
    label: str
    value: str


@dataclass(frozen=True)
class ChoiceSource:
    """Where a ``choice`` field's options come from (exactly one populated)."""
    options: Optional[List[str]] = None       # static list
    sh: Optional[str] = None                   # shell cmd; one option per stdout line
    dirs: Optional[str] = None                 # glob; directories only
    git_only: bool = False                     # dirs: keep only git repos


@dataclass(frozen=True)
class InputSpec:
    """A trigger input bound positionally from the slash-command arguments."""
    name: str
    type: str = "text"
    required: bool = False
    default: Optional[str] = None
    description: str = ""


@dataclass(frozen=True)
class FormField:
    """A field collected interactively by a ``form`` step."""
    name: str
    type: str = "text"
    description: str = ""
    source: Optional[ChoiceSource] = None
    required: bool = True
    default: Optional[str] = None


@dataclass(frozen=True)
class FormStep:
    id: str
    fields: Dict[str, FormField]


@dataclass(frozen=True)
class RunStep:
    id: str
    shell: str
    detach: bool = False
    timeout: int = DEFAULT_RUN_TIMEOUT


@dataclass(frozen=True)
class MessageStep:
    id: str
    text: str


@dataclass(frozen=True)
class PromptStep:
    id: str
    text: str


Step = Union[FormStep, RunStep, MessageStep, PromptStep]


@dataclass(frozen=True)
class CommandSpec:
    name: str
    command: str                       # slash trigger without leading slash
    description: str
    inputs: List[InputSpec]
    steps: List[Step]
    body: str = ""
    source_path: Optional[str] = None

    @property
    def has_prompt_step(self) -> bool:
        return any(isinstance(s, PromptStep) for s in self.steps)

    @property
    def has_form_step(self) -> bool:
        return any(isinstance(s, FormStep) for s in self.steps)

    @property
    def is_control_plane(self) -> bool:
        """True when the command never hands off to the agent (run/form/message only)."""
        return not self.has_prompt_step


# =========================================================================
# Frontend protocol
# =========================================================================

@runtime_checkable
class CommandFrontend(Protocol):
    """The interaction surface a frontend (CLI / gateway) implements."""

    async def collect(self, field: FormField, choices: List[Choice]) -> Optional[str]:
        """Prompt for ``field`` and return the raw user response.

        ``choices`` is non-empty for choice fields (render a picker) and empty
        for free-text fields. Return ``None`` on timeout / cancellation.
        """
        ...

    async def notify(self, text: str) -> None:
        """Post a message to the user (message steps, status, errors)."""
        ...


# =========================================================================
# Parsing / validation
# =========================================================================

def _as_str(value: Any) -> str:
    return "" if value is None else str(value)


def _parse_source(raw: Any, where: str) -> ChoiceSource:
    if not isinstance(raw, dict):
        raise CommandConfigError(f"{where}: `source` must be a mapping")
    options = raw.get("options")
    sh = raw.get("sh", raw.get("command"))
    dirs = raw.get("dirs")
    git_only = bool(raw.get("git_only", False))
    populated = [k for k, v in (("options", options), ("sh", sh), ("dirs", dirs)) if v]
    if len(populated) != 1:
        raise CommandConfigError(
            f"{where}: `source` needs exactly one of options/sh/dirs (got {populated or 'none'})"
        )
    if git_only and not dirs:
        raise CommandConfigError(f"{where}: `git_only` is only valid with `dirs`")
    if options is not None:
        if not isinstance(options, list) or not options:
            raise CommandConfigError(f"{where}: `options` must be a non-empty list")
        options = [_as_str(o) for o in options]
    return ChoiceSource(
        options=options,
        sh=_as_str(sh) if sh else None,
        dirs=_as_str(dirs) if dirs else None,
        git_only=git_only,
    )


def _parse_field(name: str, raw: Any, where: str) -> FormField:
    if not isinstance(raw, dict):
        raise CommandConfigError(f"{where}: field `{name}` must be a mapping")
    ftype = _as_str(raw.get("type", "text")).strip() or "text"
    if ftype not in _VALID_TYPES:
        raise CommandConfigError(f"{where}: field `{name}` has unknown type `{ftype}`")
    source = None
    if "source" in raw:
        source = _parse_source(raw["source"], f"{where}.{name}")
    if ftype == "choice" and source is None:
        raise CommandConfigError(f"{where}: choice field `{name}` needs a `source`")
    if ftype == "text" and source is not None:
        raise CommandConfigError(f"{where}: text field `{name}` cannot have a `source`")
    return FormField(
        name=name,
        type=ftype,
        description=_as_str(raw.get("description")),
        source=source,
        required=bool(raw.get("required", True)),
        default=_as_str(raw["default"]) if raw.get("default") is not None else None,
    )


def _parse_step(raw: Any, index: int) -> Step:
    if not isinstance(raw, dict):
        raise CommandConfigError(f"step #{index + 1} must be a mapping")
    step_id = _as_str(raw.get("id")).strip() or f"step{index + 1}"
    kinds = [k for k in ("form", "run", "message", "prompt") if k in raw]
    if len(kinds) != 1:
        raise CommandConfigError(
            f"step `{step_id}`: needs exactly one of form/run/message/prompt (got {kinds or 'none'})"
        )
    kind = kinds[0]
    if kind == "form":
        fields_raw = raw["form"]
        if not isinstance(fields_raw, dict) or not fields_raw:
            raise CommandConfigError(f"step `{step_id}`: `form` must be a non-empty mapping")
        fields = {
            fname: _parse_field(fname, fraw, f"step `{step_id}` form")
            for fname, fraw in fields_raw.items()
        }
        return FormStep(id=step_id, fields=fields)
    if kind == "run":
        run_raw = raw["run"]
        if isinstance(run_raw, str):
            run_raw = {"shell": run_raw}
        if not isinstance(run_raw, dict) or not _as_str(run_raw.get("shell")).strip():
            raise CommandConfigError(f"step `{step_id}`: `run` needs a `shell` string")
        timeout = run_raw.get("timeout", DEFAULT_RUN_TIMEOUT)
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            raise CommandConfigError(f"step `{step_id}`: `timeout` must be an integer")
        return RunStep(
            id=step_id,
            shell=_as_str(run_raw["shell"]),
            detach=bool(run_raw.get("detach", False)),
            timeout=timeout,
        )
    if kind == "message":
        return MessageStep(id=step_id, text=_as_str(raw["message"]))
    return PromptStep(id=step_id, text=_as_str(raw["prompt"]))


def _template_refs(text: str) -> List[str]:
    return _VAR_RE.findall(text or "")


def parse_command(
    frontmatter: Dict[str, Any],
    body: str = "",
    source_path: Optional[str] = None,
) -> CommandSpec:
    """Parse + validate a command spec from frontmatter. Raises CommandConfigError."""
    if not isinstance(frontmatter, dict):
        raise CommandConfigError("frontmatter must be a mapping")

    name = _as_str(frontmatter.get("name")).strip()
    if not name:
        raise CommandConfigError("command needs a `name`")

    command = _as_str(frontmatter.get("command")).strip().lstrip("/") or name
    description = _as_str(frontmatter.get("description")).strip()

    # ── Inputs (trigger-bound) ──────────────────────────────────────────
    inputs: List[InputSpec] = []
    raw_inputs = frontmatter.get("inputs") or []
    if not isinstance(raw_inputs, list):
        raise CommandConfigError("`inputs` must be a list")
    for raw in raw_inputs:
        if not isinstance(raw, dict):
            raise CommandConfigError("each input must be a mapping")
        iname = _as_str(raw.get("name")).strip()
        if not iname:
            raise CommandConfigError("each input needs a `name`")
        itype = _as_str(raw.get("type", "text")).strip() or "text"
        if itype not in _VALID_TYPES:
            raise CommandConfigError(f"input `{iname}` has unknown type `{itype}`")
        inputs.append(InputSpec(
            name=iname,
            type=itype,
            required=bool(raw.get("required", False)),
            default=_as_str(raw["default"]) if raw.get("default") is not None else None,
            description=_as_str(raw.get("description")),
        ))

    # ── Steps ───────────────────────────────────────────────────────────
    raw_steps = frontmatter.get("steps") or []
    if not isinstance(raw_steps, list) or not raw_steps:
        raise CommandConfigError("`steps` must be a non-empty list")
    steps = [_parse_step(raw, i) for i in range(len(raw_steps)) for raw in [raw_steps[i]]]

    # Duplicate step ids would make {{id.field}} ambiguous.
    ids = [s.id for s in steps]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise CommandConfigError(f"duplicate step ids: {', '.join(sorted(dupes))}")

    # A prompt step must be the LAST step (it hands off to the agent).
    for i, s in enumerate(steps):
        if isinstance(s, PromptStep) and i != len(steps) - 1:
            raise CommandConfigError("a `prompt` step must be the final step")

    _validate_references(name, inputs, steps)

    return CommandSpec(
        name=name,
        command=command,
        description=description,
        inputs=inputs,
        steps=steps,
        body=body or "",
        source_path=source_path,
    )


def _validate_references(name: str, inputs: List[InputSpec], steps: List[Step]) -> None:
    """Ensure every {{ref}} resolves to an input or an EARLIER step's output."""
    available: set[str] = {i.name for i in inputs}

    def _check(text: str, where: str) -> None:
        for ref in _template_refs(text):
            head = ref.split(".", 1)[0]
            if ref in available or head in available:
                continue
            raise CommandConfigError(
                f"{name}: {where} references unknown variable `{{{{{ref}}}}}`"
            )

    for step in steps:
        if isinstance(step, FormStep):
            # form fields don't reference variables; they DEFINE them.
            for fname in step.fields:
                available.add(f"{step.id}.{fname}")
            available.add(step.id)
        elif isinstance(step, RunStep):
            _check(step.shell, f"step `{step.id}` shell")
            # run steps expose .pid / .stdout / .exit
            available.update({f"{step.id}.pid", f"{step.id}.stdout", f"{step.id}.exit", step.id})
        elif isinstance(step, MessageStep):
            _check(step.text, f"step `{step.id}` message")
        elif isinstance(step, PromptStep):
            _check(step.text, f"step `{step.id}` prompt")


def args_hint(spec: CommandSpec) -> str:
    """Derive an args hint like ``<session> [project]`` from declared inputs."""
    parts = []
    for inp in spec.inputs:
        parts.append(f"<{inp.name}>" if inp.required else f"[{inp.name}]")
    return " ".join(parts)


def usage_text(spec: CommandSpec) -> str:
    hint = args_hint(spec)
    line = f"Usage: /{spec.command}" + (f" {hint}" if hint else "")
    if spec.description:
        line += f"\n{spec.description}"
    return line


# =========================================================================
# Choice-source resolution + matching
# =========================================================================

def resolve_choices(
    source: ChoiceSource,
    *,
    env: Optional[Dict[str, str]] = None,
    timeout: int = DEFAULT_RUN_TIMEOUT,
) -> List[Choice]:
    """Resolve a choice source to a list of Choice. Raises CommandAbort if empty."""
    choices: List[Choice] = []
    if source.options is not None:
        choices = [Choice(label=o, value=o) for o in source.options]
    elif source.dirs:
        pattern = os.path.expanduser(os.path.expandvars(source.dirs))
        matched = sorted(Path(p) for p in glob.glob(pattern))
        for p in matched:
            if not p.is_dir():
                continue
            if source.git_only and not (p / ".git").exists():
                continue
            choices.append(Choice(label=p.name, value=str(p)))
    elif source.sh:
        result = run_capture(source.sh, env=env or os.environ.copy(), timeout=timeout)
        if result.timed_out:
            raise CommandAbort(f"Option source timed out after {timeout}s.")
        for line in result.stdout.splitlines():
            line = line.strip()
            if line:
                choices.append(Choice(label=line, value=line))
    if not choices:
        raise CommandAbort("No options available to choose from.")
    return choices


def match_choice(raw: str, choices: List[Choice]) -> Optional[Choice]:
    """Resolve a user reply: exact label → 1-based index → case-insensitive label."""
    if raw is None:
        return None
    resp = raw.strip()
    for c in choices:
        if c.label == resp:
            return c
    if resp.isdigit():
        idx = int(resp) - 1
        if 0 <= idx < len(choices):
            return choices[idx]
    lc = resp.lower()
    for c in choices:
        if c.label.lower() == lc:
            return c
    return None


# =========================================================================
# Template rendering
# =========================================================================

def _flatten_context(ctx: Dict[str, Any]) -> Dict[str, str]:
    """Flatten nested step outputs into dotted keys (``pick.project``)."""
    flat: Dict[str, str] = {}
    for key, value in ctx.items():
        if isinstance(value, dict):
            for sub, subval in value.items():
                flat[f"{key}.{sub}"] = _as_str(subval)
        else:
            flat[key] = _as_str(value)
    return flat


def _render(template: str, ctx: Dict[str, Any], *, quote: bool) -> str:
    flat = _flatten_context(ctx)

    def repl(m: re.Match) -> str:
        key = m.group(1)
        val = flat.get(key, "")
        return shlex.quote(val) if quote else val

    return _VAR_RE.sub(repl, template or "")


def render_shell(template: str, ctx: Dict[str, Any]) -> str:
    """Render a shell template, shlex.quote-ing every interpolated value.

    The template's own shell operators (``&&``, ``cd``, pipes) are
    operator-authored and trusted; only the substituted *values* are quoted,
    which is the injection defense against untrusted argument input.
    """
    return _render(template, ctx, quote=True)


def render_text(template: str, ctx: Dict[str, Any]) -> str:
    """Render a plain-text template (messages / prompts) with raw substitution."""
    return _render(template, ctx, quote=False)


# =========================================================================
# Spawn helpers
# =========================================================================

@dataclass
class CaptureResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    timed_out: bool = False
    error: Optional[str] = None


def run_capture(command: str, *, env: Dict[str, str], timeout: int) -> CaptureResult:
    """Run ``command`` synchronously, capturing output."""
    try:
        proc = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout, env=env,
        )
        return CaptureResult(
            stdout=proc.stdout or "", stderr=proc.stderr or "",
            exit_code=proc.returncode,
        )
    except subprocess.TimeoutExpired:
        return CaptureResult(timed_out=True, error=f"timed out after {timeout}s")
    except Exception as exc:  # noqa: BLE001
        return CaptureResult(exit_code=1, error=str(exc))


async def run_capture_async(command: str, *, env: Dict[str, str], timeout: int) -> CaptureResult:
    """Run ``command`` asynchronously, capturing output."""
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return CaptureResult(timed_out=True, error=f"timed out after {timeout}s")
        return CaptureResult(
            stdout=(stdout or b"").decode(errors="replace"),
            stderr=(stderr or b"").decode(errors="replace"),
            exit_code=proc.returncode or 0,
        )
    except Exception as exc:  # noqa: BLE001
        return CaptureResult(exit_code=1, error=str(exc))


def spawn_detached(command: str, *, env: Optional[Dict[str, str]] = None) -> int:
    """Spawn ``command`` detached (own session, no stdio) and return its PID.

    ``env=None`` inherits the full process environment — required for tools like
    the ``claude`` CLI that need their auth/keychain access. This is a deliberate
    relaxation versus the sanitized env used for captured commands; the command
    template is operator-authored and trusted.
    """
    proc = subprocess.Popen(
        command,
        shell=True,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    return proc.pid


# =========================================================================
# Executor
# =========================================================================

def bind_inputs(spec: CommandSpec, arg_string: str) -> Dict[str, str]:
    """Bind trigger inputs positionally from the slash-command argument string.

    Raises CommandUsageError when a required input is missing or args don't parse.
    """
    try:
        tokens = shlex.split(arg_string or "")
    except ValueError:
        raise CommandUsageError(usage_text(spec))

    ctx: Dict[str, str] = {}
    for i, inp in enumerate(spec.inputs):
        if i < len(tokens):
            ctx[inp.name] = tokens[i]
        elif inp.default is not None:
            ctx[inp.name] = inp.default
        elif inp.required:
            raise CommandUsageError(usage_text(spec))
        else:
            ctx[inp.name] = ""
    return ctx


async def execute_steps(
    spec: CommandSpec,
    ctx: Dict[str, Any],
    frontend: CommandFrontend,
    *,
    env_capture: Optional[Dict[str, str]] = None,
    env_detach: Optional[Dict[str, str]] = None,
    redact: Optional[Any] = None,
) -> Optional[PromptHandoff]:
    """Run a command's steps. User-facing output goes through ``frontend.notify``.

    Returns a :class:`PromptHandoff` when the workflow ends in a ``prompt`` step;
    otherwise ``None``. ``ctx`` must already contain the bound trigger inputs.
    """
    env_capture = env_capture if env_capture is not None else os.environ.copy()

    for step in spec.steps:
        if isinstance(step, FormStep):
            out: Dict[str, str] = {}
            for fname, fld in step.fields.items():
                value = await _collect_field(fld, frontend, env_capture)
                out[fname] = value
            ctx[step.id] = out

        elif isinstance(step, RunStep):
            command = render_shell(step.shell, ctx)
            if step.detach:
                try:
                    pid = spawn_detached(command, env=env_detach)  # None → inherit full env
                except Exception as exc:  # noqa: BLE001
                    raise CommandAbort(f"Failed to start: {exc}")
                ctx[step.id] = {"pid": str(pid), "stdout": "", "exit": "0"}
            else:
                result = await run_capture_async(command, env=env_capture, timeout=step.timeout)
                if result.timed_out:
                    raise CommandAbort(f"Step `{step.id}` timed out after {step.timeout}s.")
                stdout = result.stdout
                if redact is not None and stdout:
                    try:
                        stdout = redact(stdout)
                    except Exception:  # noqa: BLE001
                        pass
                ctx[step.id] = {
                    "pid": "", "stdout": stdout.strip(), "exit": str(result.exit_code),
                }

        elif isinstance(step, MessageStep):
            await frontend.notify(render_text(step.text, ctx))

        elif isinstance(step, PromptStep):
            return PromptHandoff(render_text(step.text, ctx))

    return None


async def _collect_field(
    fld: FormField,
    frontend: CommandFrontend,
    env_capture: Dict[str, str],
) -> str:
    """Resolve one form field's value (auto-select / interactive collect)."""
    if fld.type == "choice":
        assert fld.source is not None
        choices = resolve_choices(fld.source, env=env_capture)
        if len(choices) == 1:
            return choices[0].value
        raw = await frontend.collect(fld, choices)
        if raw is None:
            raise CommandAbort(f"No selection for `{fld.name}` — cancelled.")
        chosen = match_choice(raw, choices)
        if chosen is None:
            raise CommandAbort(f"Couldn't match `{raw}` to an option for `{fld.name}`.")
        return chosen.value
    # text field
    raw = await frontend.collect(fld, [])
    if raw is None:
        if fld.default is not None:
            return fld.default
        if fld.required:
            raise CommandAbort(f"No value for `{fld.name}` — cancelled.")
        return ""
    return raw
