# AI Style Guide

A single reference to hand an AI coding assistant — paste it in, or point
the assistant at this page — before it touches this codebase. Its job is
narrow: keep contributions consistent no matter which human, or which AI
tool, produced them, so the codebase doesn't slowly accumulate several
different styles and several different toolboxes for the same job.

## The rule this page exists to enforce

Don't introduce a second way of doing something this codebase already does
one way. That's [Golden Rules](golden-rules.md#write-pythonic-code--dont-fight-the-language)'s
"Pythonic, boring, stock-standard Python" rule, applied specifically to
AI-suggested tools, patterns, and dependencies — the place an inconsistent
new style is most likely to sneak in unnoticed.

## Established stack — use what's already here

| Job | What this project already uses | Not: |
| --- | --- | --- |
| CLI commands | [`click`](https://click.palletsprojects.com/) | `argparse`, `typer`, hand-rolled `sys.argv` parsing |
| Structured data | `dataclasses`, `typing.Protocol` contracts | plain dicts as pseudo-objects, custom base-class hierarchies |
| Error handling | EAFP, specific `except` tuples | broad `except Exception`, error-code return values |
| Testing | `pytest` | `unittest` as the *primary* style for new tests, custom test runners |
| Logging | stdlib `logging` | `print()`, a new logging library |
| CLI progress/output | `rich` | a second progress-bar or console-formatting library |
| Packaging/versioning | `setuptools_scm` (git tags), PyInstaller, Inno Setup | hand-maintained version strings, a different bundler |
| Docs | `mkdocs` + `mkdocs-material` | a second docs generator |

Before adding a new dependency, check `requirements.txt` first — if
something already does the job, use that.

## Style specifics

- **Formatting/linting**: `ruff` — 100-character lines, double-quote
  strings. Let it format on save; don't hand-format around it.
- **Type hints** on every function signature — this is what makes
  `pyright`/Pylance catch mistakes at edit time.
- **Naming**: `snake_case` for functions/variables, `PascalCase` for
  classes, `UPPER_SNAKE_CASE` for module-level constants.
- **f-strings** for string formatting, not `.format()` or `%`.
- **Minimal docstrings** — a line or two at most, only where the *why*
  isn't obvious from the code itself. Well-named functions and classes do
  most of the explaining; don't add a docstring that just restates the
  function name.

## Patterns to reuse, not reinvent

- **EAFP** for error handling, with specific exception types named in the
  `except` clause — [Golden Rules](golden-rules.md#ask-forgiveness-not-permission-eafp).
- **`dataclasses`**, frozen where the object shouldn't mutate after
  construction.
- **Thin CLI, fat `processing/`** — CLI files read input and call into
  `processing/`; the actual logic doesn't live in the CLI file.

## No flashy new patterns

Specifically avoid, unless there's no working alternative and it's been
discussed first: metaclasses, monkey-patching, deeply nested
comprehensions or clever one-liners that trade readability for brevity,
new decorator-based magic (`@deprecated` is the one established exception
— [Golden Rules](golden-rules.md#mark-old-code-deprecated--dont-just-delete-it-or-leave-it-silently)),
and speculative abstractions or config options for a use case that doesn't
exist yet — [Golden Rules](golden-rules.md#write-code-for-today-not-for-a-hypothetical-future).

If a genuinely better pattern seems worth introducing, propose it
explicitly, as its own step, with the tradeoff stated — never slip it in
as a side effect of an unrelated change.

## One task, one change

An assistant may touch as many files as a task genuinely needs, but
shouldn't bundle unrelated changes or introduce multiple new patterns in
one go — anything outside the task gets suggested, not slipped in.

## Teaching examples

If an assistant is asked for example code to explain a concept rather than
to solve the actual task, keep examples in a clearly separate, obviously
fictional domain unrelated to this project's own work (this repo's
`AGENTS.md`, at the workspace root, illustrates this with an everyday
domain like library checkouts, bus schedules, or a small inventory — pick
whatever fits the concept, but stay in one domain per explanation) — so an
example never gets mistaken for, or pasted in as, real production code.

## What an assistant may run

An AI assistant may run the tests, `ruff`, `pyright`, `mkdocs build`,
`build.ps1` (any mode — it writes only under the gitignored `build_output/`
plus an editable install into `.venv`), `pip install` into the project
`.venv`, and the project's own CLIs on **synthetic data or the cleared
`safe_data_source/` folder only**. Anything
that would touch real data, git history, or a remote system stays a
suggestion for the user to run — see `AGENTS.md`'s guardrails.

## Restricted folders

Separate from authorship style: `.ai_restricted` (repo root, if present) is
a human-maintained, `.gitignore`-style list of folders no AI assistant may
read or write — typically example/sample data the user doesn't want an AI
touching. Absolute, and not affected by any mode or command — see `AGENTS.md`'s
restricted-folders guardrail.

## Using this page

Point any AI assistant here before it looks at anything else in this
repo — as a pasted-in reference, or via whatever project config file your
tool reads (`AGENTS.md`, `CLAUDE.md`, `.cursorrules`,
`.github/copilot-instructions.md`, …). This repo's own `AGENTS.md` already
does the equivalent for its interaction style; this page is the equivalent
for code style and toolbox choices.
