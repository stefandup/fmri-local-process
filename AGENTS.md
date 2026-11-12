---
alwaysApply: true
---
## Working Mode: build fast, explain briefly

This is a **get-it-built** project, not a learning exercise. The goal is working code as
quickly as possible, with the assistant doing the implementation. The user still wants to
learn along the way, so:

* **Do the work.** Implement what's asked directly — create and edit files, across as many
  files as the task genuinely needs. No mini-challenges, no per-edit consent gate.
* **Explain briefly.** After a change, give a short summary: what changed, where, and the one
  or two *why* points worth knowing (a non-obvious design choice, a library idiom, a
  pitfall avoided). Skip explanations of the obvious.
* **Verify before reporting.** Run the relevant tests, `ruff check`, `ruff format` and
  `pyright` on what you touched, and fix what they flag. Report results honestly — never
  claim something ran or passed if it didn't.
* **Ask only at real forks.** Ask when a decision is genuinely the user's (scope, an
  architecture choice, a new dependency, anything hard to reverse). Otherwise pick the
  sensible default that fits the [AI Style Guide](docs/ai-style-guide.md) and say so.
* **Stay in scope.** Do what was asked plus what it strictly needs. No drive-by refactors,
  speculative features or config options for hypothetical futures — mention them as
  suggestions instead.

---

## Quick Commands the User Can Use

* **TEACH: <topic>** → switch to the step-by-step tutor loop for that topic (below) until
  the user says **BUILD** or the topic is done.
* **BUILD** → back to the default build-fast mode.
* **PLAN: <task>** → outline the approach and the files that will change, then stop and wait
  before implementing. Use for larger or risky changes.
* **QUICK: <question>** → a succinct explanation plus a brief general example; no edits.
* **REVIEW** / **EVALUATE** → run Review Mode (below).

---

## Teach Mode (opt-in only, via TEACH)

1. **Overview** — 1–3 lines on the goal of this single step and why it matters.
2. **Mini-challenge** — a tiny task for the user to attempt first.
3. **Hints** — on request, 1–2 escalating hints (*gentle* → *direct*).
4. **Consent gate** — ask before proposing a diff or running anything.
5. **Stop & wait** — *"Proceed to the next single step? (yes/no/adjust)"*

**Example domain rule**: illustrate with a simple, everyday domain unrelated to the user's
real work (physiology, VR, neuroimaging, etc.) — e.g. library checkouts, bus schedules, a
small inventory. Stay in one domain per explanation and use neutral-but-meaningful names
(`process_item()`, not `foo`/`bar`). The point is to show the pattern, not to hint at the
answer to the user's actual problem.

---

## Review Mode

Review Mode is a **separate, periodic mode**. It only runs when the user explicitly asks for
**REVIEW** or **EVALUATE**.

When triggered, include:
- Run the tests in `tests/` to confirm that everything is running smoothly.
- Check `docs/` (and repo root) for a `*_milestones.md` file. If found, ground the estimate
  in that recorded history instead of only the current diff, and note any real shift in
  approach since the last entry.
- a brief estimate of the user's current level for this project, using the
  **Dreyfus model of skill acquisition**: Novice, Advanced Beginner, Competent, Proficient,
  or Expert, with a short justification grounded in that stage's behavioral markers
- a best-practices score against Real Python's Python code-quality guidance, especially
  functionality, readability, maintainability, robustness, testability, naming, modular
  design, style, formatting, and appropriate tooling
- a fun second read using the **Monty Python Coder Rank** below, alongside (not instead of)
  the Dreyfus estimate
- one gentle next improvement that would move the score upward

### Monty Python Coder Rank (fun, supplementary — not a replacement for Dreyfus)

| Rank | Sketch | What they actually do |
|---|---|---|
| 1 | Dead Parrot | Ships code, insists it works, it's just "resting" (i.e. hasn't been run) |
| 2 | Spanish Inquisition | `except Exception: pass` everywhere — catches things nobody saw coming |
| 3 | Ministry of Silly Walks | Gets the job done, just very unidiomatically |
| 4 | Bridge of Death | Measures before optimizing — asks "what's the actual bottleneck?" first |
| 5 | Knights Who Say Ni | Won't approve a PR without a shrubbery — type hints, tests, docstrings, non-negotiable |
| 6 | The Architect | Designs the load-bearing walls first — sees the whole system before writing the first function |

### Milestone Tracking

After completing a REVIEW, append one new row to `docs/<project>_milestones.md` (create it on
first use): date, commit hash (or "WIP tip" if uncommitted), a short milestone label, and a
one-line note on what it demonstrates. Only add a row for a real shift in approach.

---

## Code Standards (always apply)

* Follow [Golden Rules](docs/golden-rules.md) and the [AI Style Guide](docs/ai-style-guide.md):
  established stack only, `Path` not strings, EAFP with specific exceptions, type hints on
  every signature, thin CLI / logic in `processing/`, no flashy patterns.
* **Tests alongside code**: new processing logic gets a `pytest` test in the same change,
  using synthetic data (small generated arrays / NIfTI images). Tests may also use
  `safe_data_source/`, but skip cleanly when it's absent, since it's gitignored and won't
  exist on a fresh clone.
* Prefer the smallest change that fully solves the task; keep changes reversible.

---

## Guardrails (always apply, in every mode)

* **Cleared data — `safe_data_source/`**: everything the user puts in `safe_data_source/` (repo root) is cleared for AI use. The assistant may open, read, load, inspect, plot, and run the CLIs, tests and GUI against it, and use it for development and debugging. It must never be modified, moved or deleted. Write all outputs elsewhere (e.g. `results/` or a temp folder). Only the user adds to it. Cleared status covers that folder only, not copies of real data anywhere else. If a file in it looks like it holds personal identifiers (names, dates of birth, un-defaced faces in a header/sidecar), stop and flag it rather than using it. `.ai_restricted` still wins if it ever lists this folder.

* **Data guardrail (critical)**: Outside `safe_data_source/`, never open, load, read, or otherwise use the user's subject/participant data or anything that could be sensitive (physiology, VR, neuroimaging recordings, personal identifiers, etc.) — even just to check format. File/column names, headers, and directory structure are fine to look at. If a task seems to need real data content, use `safe_data_source/`, generate synthetic data, or ask the user to add suitable data to `safe_data_source/`. **Exception**: running the existing `tests/` suite via `pytest` is allowed even where a test loads real, gitignored data to produce its pass/fail result — that's the test code using the data internally, not this assistant opening it. What stays absolute: never independently open, `cat`, Read, print, or otherwise surface the contents of real-data files, and never include their contents (or anything beyond pytest's normal pass/fail/assertion/traceback output) in a message, file, or upload. If a traceback or `-v`/`-s` output looks like it's dumping real data values (a dataframe repr, raw signal numbers, voxel values) rather than just status text, stop and summarize instead of pasting it verbatim.

* **Restricted-folders guardrail (critical, absolute — not affected by any mode or command)**: Check `.ai_restricted` (repo root, if present) before reading or writing anywhere in the repo. It's a plain-text, human-maintained list of folders — one per line, `.gitignore`-style — that this assistant must never open, read, write to, or otherwise access, for any reason. This is where the user stores example/sample data they don't want an AI touching. `.ai_restricted` itself is covered by the same restriction: read it to know what's listed, but never create, edit, or delete it — only a human maintains it.

* **Commands**: the assistant may run tests, `ruff`, `pyright`, `mkdocs build`, `build.ps1`, `pip install` into the project `.venv`, and the project's own CLIs **on synthetic data or `safe_data_source/` only**. Running anything against other real data (including the processing CLIs) means suggesting the command for the user to run. Never run `git commit`/`push`, or anything that touches remote systems, without an explicit request.

* Never claim to have run code, tests, or linters that weren't actually run.
