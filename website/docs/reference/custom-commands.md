---
sidebar_position: 3
title: "Custom Commands"
description: "Slack-style workflow files that turn a slash command into typed inputs, interactive pickers, and steps"
---

# Custom Commands

Custom commands let you define your own slash commands as **workflow files** — no
code changes, no plugin scaffolding. They are modeled on Slack's Workflow Builder
and decompose into three parts:

- **Trigger** — the slash command and how message arguments bind to *inputs*.
- **Inputs collected via forms** — *typed* fields. Any value the trigger didn't
  supply is collected interactively. A "project picker" isn't a special feature:
  it's a `choice` field whose value wasn't provided, so a form asks for it.
- **Steps + variables** — ordered actions (`run`, `message`, `prompt`) that
  reference earlier values with `{{variable}}` interpolation.

They sit between [Quick Commands](./slash-commands.md#quick-commands) (one shell
line or alias) and [Skills](../guides/work-with-skills.md) (loaded into the
model's context). Reach for a custom command when you need typed arguments, an
interactive picker, or more than one step.

## Where they live

Drop a `COMMAND.md` file under `~/.hermes/commands/<name>/`:

```
~/.hermes/commands/
  claude/
    COMMAND.md
```

You can point at additional directories with `commands.external_dirs` in
`~/.hermes/config.yaml` (mirrors `skills.external_dirs`), and disable specific
commands with `commands.disabled`:

```yaml
commands:
  external_dirs:
    - ~/team-commands
  disabled:
    - some-command
```

Files are scanned at startup — **editing a command requires a restart** (or a
fresh CLI session). There is no hot reload.

## File format

A `COMMAND.md` is YAML frontmatter (the workflow schema) plus an optional
markdown body (help text). Here is the complete worked example — a `/claude`
command that picks a project and launches a remote-controllable
[Claude Code](https://claude.com/claude-code) session in it:

```markdown
---
name: claude
description: Spawn remote-controlled Claude Code in a project
# command: /claude          # optional; defaults to /<name>
inputs:                      # bound positionally from slash-command args
  - name: session
    type: text
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
      shell: cd {{pick.project}} && claude --permission-mode bypassPermissions --worktree {{session}} --remote-control {{session}}
      detach: true
  - message: "🚀 Claude Code launched on {{pick.project}} — session {{session}}"
---
Spawn a remote-controllable Claude Code session in one of your repos.
Usage: `/claude <session-name>`
```

Run it from the CLI or any messaging platform:

```
/claude my-feature
```

`session` binds from the argument. `project` is a `choice` with no supplied
value, so the command renders a picker (inline buttons on Telegram/Discord, a
numbered list elsewhere). Once you pick, the `run` step launches the process
detached and the `message` step confirms.

## Schema

### Top level

| Key | Required | Description |
|-----|----------|-------------|
| `name` | yes | Command name (also the default slash trigger). |
| `command` | no | Override the slash trigger; defaults to `/<name>`. |
| `description` | no | Shown in `/help`, `/commands`, and platform menus. |
| `inputs` | no | Trigger inputs, bound positionally from the message args. |
| `steps` | yes | Ordered list of steps (at least one). |

### Inputs

Each input is bound from the slash-command arguments in order
(`/claude my-feature` → `session = "my-feature"`).

| Key | Description |
|-----|-------------|
| `name` | Variable name; referenced as `{{name}}`. |
| `type` | `text` (default) or `choice`. |
| `required` | If true and unbound, the command returns its usage text. |
| `default` | Value used when the argument is omitted. |
| `description` | Used in the generated usage/`args_hint`. |

### Steps

Each step has exactly one of `form`, `run`, `message`, or `prompt`, plus an
optional `id` (used to namespace its outputs).

**`form`** — collect typed fields interactively. Each field:

| Key | Description |
|-----|-------------|
| `type` | `text` or `choice`. |
| `description` | Prompt text / picker title. |
| `source` | For `choice`: where options come from (below). |
| `required` | Defaults to true. |
| `default` | Fallback value. |

A field's value is exposed as `{{step_id.field_name}}`.

**`run`** — run a shell command.

| Key | Description |
|-----|-------------|
| `shell` | The command template. |
| `detach` | If true, spawn detached (own session, no stdio) and report the PID. |
| `timeout` | Capture-mode timeout in seconds (default 30; ignored when detached). |

Outputs: `{{step_id.pid}}` (detached) or `{{step_id.stdout}}` / `{{step_id.exit}}`
(captured).

**`message`** — post text to the user (template).

**`prompt`** — hand the rendered text to the agent as the turn's message. A
`prompt` step must be the **last** step.

### Choice sources

A `choice` field/input draws its options from exactly one source:

| Source | Behavior |
|--------|----------|
| `options` | A static list. |
| `sh` (or `command`) | A shell command; one option per stdout line. |
| `dirs` | A glob expanded to **directories only**. Add `git_only: true` to keep only git repos. The label is the basename; the substituted value is the absolute path. |

A source that yields exactly one option auto-selects (no prompt). Zero options
aborts the command with an error.

## Variables and quoting

Reference values with `{{...}}`:

- Trigger inputs are flat: `{{session}}`.
- Step outputs are namespaced by step id: `{{pick.project}}`, `{{launch.pid}}`.

In `shell` templates, **every interpolated value is shell-quoted** — this is the
injection defense for untrusted argument input. The template's own shell
operators (`&&`, `cd`, pipes) are operator-authored and run as written. Because
each value is quoted as a single token, use `{{1}}`-style separate inputs rather
than expecting one value to expand into multiple shell words.

In `message` and `prompt` templates, values are substituted as plain text (no
quoting).

## Execution surfaces

- **CLI** — runs synchronously; pickers are numbered `input()` prompts.
- **Messaging gateway** — control-plane commands (no `prompt` step) run in the
  background and reply via follow-up messages, so the picker can wait on your
  reply without blocking. Pickers reuse the platform `send_clarify` primitive
  (the same one the `clarify` tool uses), so Telegram and Discord get native
  inline buttons and every other platform gets a numbered-text fallback — no
  per-platform work needed. Control-plane commands also dispatch **while an
  agent is running**, since they don't touch the agent's state.

## Security notes

- `detach: true` spawns with the **full environment inherited** (including API
  keys) — required for tools like the `claude` CLI that need their auth. Captured
  (non-detached) commands in the gateway run with a sanitized environment and
  redacted output. Command files are operator-authored and trusted; treat
  `detach` like any shell script you'd run yourself.
- Custom commands respect the same per-platform slash-command access control as
  built-ins (`allow_admin_from` / `user_allowed_commands`).

## Limitations (v1)

- A `prompt` step must be last. In the gateway, prompt commands that also need an
  interactive form aren't supported yet — split them into two commands.
- No hot reload — restart after editing a command file.
- When two `dirs` matches share a basename, the numbered pick disambiguates;
  label matching takes the first.
