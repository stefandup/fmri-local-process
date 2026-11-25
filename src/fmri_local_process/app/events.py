"""A Qt-signal look-alike without Qt."""

from collections.abc import Callable


class Event:
    """A list of callbacks: `ev.connect(f)`, then `ev.emit(x)` calls `f(x)`.

    Spelled like a Qt Signal, so the window's code is unchanged. Only emit on the UI thread;
    a job on a worker thread reports through its listener instead."""

    def __init__(self) -> None:
        self._callbacks: list[Callable[..., object]] = []

    def connect(self, callback: Callable[..., object]) -> None:
        self._callbacks.append(callback)

    def emit(self, *args: object) -> None:
        for callback in list(self._callbacks):  # a callback may connect another
            callback(*args)
