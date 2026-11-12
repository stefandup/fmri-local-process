# Golden Rules

A handful of rules of thumb this codebase follows. Worth knowing before you
write new code here.

If you're using an AI assistant, also see the [AI Style Guide](ai-style-guide.md) —
it turns these rules into a concrete established-stack/avoid-list an
assistant can be pointed at directly.

## Write Pythonic code — don't fight the language

**Pythonic** means writing code the way Python itself is designed to be
written — using the language's own idioms and standard library, instead
of patterns carried over wholesale from another language you happen to
know better (C-style index loops, Java-style getters/setters). Python has
a short, semi-official statement of this philosophy — run `import this`
in any Python shell to see it ("The Zen of Python"). A few lines from it
that matter most here: *"Readability counts."* *"Explicit is better than
implicit."* *"There should be one — and preferably only one — obvious way
to do it."*

Concrete examples, contrasted with the non-Pythonic version scientists
coming from another language often reach for first:

```python
# Not Pythonic — manual index loop, a C/Java habit
squared_totals = []
for i in range(len(orders)):
    squared_totals.append(orders[i]["total"] ** 2)

# Pythonic — list comprehension
squared_totals = [order["total"] ** 2 for order in orders]
```

```python
# Not Pythonic — manual index tracking
i = 0
for order in orders:
    print(i, order)
    i += 1

# Pythonic — enumerate()
for i, order in enumerate(orders):
    print(i, order)
```

```python
# Not Pythonic — length check standing in for truthiness
if len(kitchen_queue) > 0:
    ...

# Pythonic — empty containers are already falsy
if kitchen_queue:
    ...
```

```python
# Not Pythonic — manual string building
message = "Order " + str(order_id) + " is " + status

# Pythonic — f-string
message = f"Order {order_id} is {status}"
```

The [EAFP rule](#ask-forgiveness-not-permission-eafp) below is itself a
Pythonic-vs-not example, not a separate idea — `try`/`except` over manual
precondition checks is this same principle applied to error handling.

### Why this matters *more*, not less, for non-programmers

It's tempting to assume idiomatic Python is a "once you already know
Python" nicety, and that someone new to coding is better off writing
whatever gets the job done, however verbose. We think the opposite:

- **Every beginner-facing Python resource teaches the idiomatic way.**
  Tutorials, official docs, and Stack Overflow answers all model list
  comprehensions, `with` blocks, f-strings, and so on. Code that goes
  against the grain cuts you off from being able to look up your way
  through a problem — the answer you find won't look like the code in
  front of you.
- **Non-Pythonic code carried over from another language brings that
  language's bugs with it.** Manual index loops are exactly where
  off-by-one errors live; `if len(x) > 0` and `if x` quietly diverge the
  moment `x` turns out to be `None` instead of just empty.
- **Consistency makes a codebase learnable.** Reading Pythonic code
  elsewhere in a project and then writing non-Pythonic code yourself
  doubles the number of styles you have to hold in your head at once.

### Prefer boring, stock-standard Python

Related but distinct: even within idiomatic Python, prefer the plain,
well-known feature over the clever or exotic one. Standard library over a
dependency that reinvents it; a straightforward function over a
metaclass or decorator trick doing the same job less legibly;
`pathlib.Path` over hand-rolled string path-joining.

Reasons this earns its place as a rule, not just a stylistic preference:

- **Boring code fails predictably.** When something breaks in a script
  built from stock `for` loops and `if` statements, the error is usually
  exactly where it looks like it is. Clever code — deeply nested
  comprehensions, `__getattr__` magic, monkey-patching — can make the
  actual failure point invisible.
- **Debuggable by more people.** Someone a few months into Python can
  step through boring code and understand every line. That's not true of
  code leaning on rarely-used corners of the language — which, in a lab
  where people rotate through every few years, is exactly the code most
  likely to get quietly abandoned rather than fixed.
- **It ages well.** Flashy patterns tend to be trend-driven; basic
  control flow and the standard library have stayed the same for two
  decades and will likely outlast whichever clever technique looked good
  this year.

## Always use `Path`, never strings, for files and folders

Every folder or file passed through this codebase — a CLI argument, an
input/output folder, a filename handed between functions — should be a
`pathlib.Path`, never a bare `str`. This is a hard rule, not just a style
preference.

```python
# Not this — string in, string out, joined by hand
def find_participant_file(data_folder: str, participant_id: str) -> str:
    return os.path.join(data_folder, f"{participant_id}.csv")


# This — Path in, Path out
def find_participant_file(data_folder: Path, participant_id: str) -> Path:
    return data_folder / f"{participant_id}.csv"
```

- **String path-building is where platform-separator bugs come from** —
  `str(a_path).split("_")` silently breaks on Windows because `str()`
  renders backslashes that a string-oriented split doesn't expect. Use
  `.name`, `.stem`, `.parts`, or `/` instead of manual string
  splitting/joining, and the platform's separator is never something your
  code has to reason about.
- **CLI entry points should hand back `Path` from the start**, via
  `click.Path(..., path_type=Path)`, rather than a plain string that gets
  wrapped in `Path(...)` partway through the function.

## Ask forgiveness, not permission (EAFP)

Python has a strong, named idiom for how to handle things that might go
wrong: **EAFP** — "Easier to Ask Forgiveness than Permission." The idea:
just *try* the thing, and handle the specific exception if it fails, rather
than checking every precondition first and only proceeding if they all
pass (that alternative style has its own name too: **LBYL**, "Look Before
You Leap").

```python
# LBYL — check permission first
if item_type in self.items:
    return self.items[item_type]
else:
    raise ValueError(f"Store has no item of type {item_type.__name__}")

# EAFP — ask forgiveness instead
try:
    return self.items[item_type]
except KeyError as e:
    raise ValueError(f"Store has no item of type {item_type.__name__}") from e
```

Both versions do the same thing here, but EAFP is the more idiomatic —
and often faster — Python style: the "normal" path stays uncluttered by
precondition checks, and the failure handling lives in one place (the
`except` block) instead of being scattered through `if`/`else` branches.

That only works well combined with the next rule, though — EAFP still
needs the `except` to name *specific* exceptions, not swallow everything.

## Catch specific exceptions, not broad ones

Every `except` block names exact exception types, e.g.:

```python
except (ValueError, FileNotFoundError) as e:
    ...
```

never a bare `except:` or `except Exception:`. Widening an `except` tuple
to make an error go away doesn't fix anything — it hides the symptom, and
swallows any *other* unrelated error of that type too.

**The rule:** if you're tempted to widen an `except` tuple to make an
error go away, that's usually a sign you need an explicit guard
(`if x is None: ...`) instead — not a broader `except`.

## Write code for today, not for a hypothetical future

> Do not rewrite everything at once. Preserve working behaviour and improve
> one structural issue at a time.

In practice: don't add configuration options, abstractions, or flexibility
for a use case that doesn't exist yet. If a genuine second use case shows
up later, that's when the abstraction earns its keep.

## Mark old code `@deprecated` — don't just delete it or leave it silently

When code is superseded but not yet safe to delete (e.g. other code still
calls it, or you want a record of why it was replaced), mark it with
`typing_extensions.deprecated` instead of quietly leaving it in place or
removing it outright:

```python
from typing_extensions import deprecated

@deprecated("Superseded by schedule_next_bus(); remove once callers migrate")
def find_next_bus(...):
    ...
```

The decorator doesn't stop the function from running; it makes type
checkers (like `pyright`) flag any call site with a warning, showing your
message as the reason. That gives anyone still calling it — including
future-you — an explicit, visible reason to migrate, instead of the
function just quietly rotting or vanishing without a trace.
