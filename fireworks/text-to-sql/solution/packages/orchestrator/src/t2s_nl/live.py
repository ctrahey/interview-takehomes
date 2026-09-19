"""The live activity line: what `t2s-chat` draws while a step is running (D14).

Chris's complaint was not that a 7-second sample-data generation is slow. It is
that the terminal is *silent* for 7 seconds, so there is no way to tell a slow
step from a hung one. This module is the answer: an
:class:`~t2s_nl.activity.ActivityListener` that prints

    ⋯ generating sample data  3.4s

with the seconds ticking in place, and resolves it on completion to

    ✓ generating sample data  7.0s

**It cannot slow the work.** The ticking happens on a daemon thread that does
nothing but redraw one line; the orchestrator's thread is never joined, never
locked against for longer than a `print`, and never waits on the renderer. If
this module raised on every call the turn would still complete --
`ActivityEmitter` swallows listener exceptions by design.

On a non-tty (a pipe, a CI log, `--ask` redirected to a file) the animation is
pointless and would emit a wall of carriage returns, so the display degrades to
one plain line per step. That degradation is also what makes the transcripts in
the report readable.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import TextIO

from t2s_nl.activity import ActivityRecord
from t2s_nl.render import colorize

__all__ = ["LiveActivityDisplay", "format_end", "should_animate"]

#: How often the elapsed counter is redrawn. Fast enough to read as live, slow
#: enough that a thousand-step session costs nothing measurable.
TICK_SECONDS = 0.1

#: Steps that finish faster than this never get a line at all: a 4 ms
#: `corrective.record` flashing past is noise, and noise is what made the
#: interesting lines hard to see in the first place.
MIN_VISIBLE_SECONDS = 0.35

_SPINNER = "⋯⋰⋯⋱"


def should_animate(stream: TextIO | None = None) -> bool:
    out = stream or sys.stdout
    if os.environ.get("T2S_NO_LIVE"):
        return False
    try:
        return bool(out.isatty())
    except (AttributeError, ValueError):
        return False


def format_end(record: ActivityRecord) -> str:
    """The resolved line for a finished step: mark, label, duration."""
    seconds = (record.duration_ms or 0) / 1000.0
    if record.status == "ok":
        mark = colorize("green", "✓")
    elif record.status == "refused":
        mark = colorize("yellow", "⊘")
    else:
        mark = colorize("red", "✗")
    return f"  {mark} {record.label}  {colorize('dim', f'{seconds:.1f}s')}"


class LiveActivityDisplay:
    """Draws one in-place line per running step; resolves it when the step ends.

    One step is visible at a time, which matches how the orchestrator works: a
    plan executes its directives in order and each directive's steps are
    sequential. A nested step (a repair inside a generation) replaces the line
    and the outer step's own resolution follows it, so the transcript reads as
    an ordered narrative rather than a re-entrant tree.
    """

    def __init__(self, stream: TextIO | None = None, *, animate: bool | None = None) -> None:
        self.stream = stream or sys.stdout
        self.animate = should_animate(self.stream) if animate is None else animate
        self._lock = threading.Lock()
        self._current: ActivityRecord | None = None
        self._started = 0.0
        self._frame = 0
        self._painted = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- ActivityListener --------------------------------------------------
    def on_begin(self, record: ActivityRecord) -> None:
        with self._lock:
            self._current = record
            self._started = time.monotonic()
            self._frame = 0
            self._painted = False
        if not self.animate:
            return
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._tick, name="t2s-live", daemon=True)
            self._thread.start()

    def on_end(self, record: ActivityRecord) -> None:
        with self._lock:
            painted = self._painted
            self._current = None
            self._painted = False
            if painted:
                self._erase()
            visible = painted or (record.duration_ms or 0) / 1000.0 >= MIN_VISIBLE_SECONDS
            if visible:
                print(format_end(record), file=self.stream, flush=True)

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=TICK_SECONDS * 3)
        self._thread = None

    # -- internals ---------------------------------------------------------
    def _tick(self) -> None:
        """Redraw the running line until it ends. Daemon; never joined by work."""
        while not self._stop.wait(TICK_SECONDS):
            with self._lock:
                record = self._current
                if record is None:
                    continue
                elapsed = time.monotonic() - self._started
                if elapsed < MIN_VISIBLE_SECONDS:
                    continue
                self._frame += 1
                spin = _SPINNER[self._frame % len(_SPINNER)]
                line = f"  {colorize('cyan', spin)} {record.label}  "
                line += colorize("dim", f"{elapsed:.1f}s")
                self._paint(line)

    def _paint(self, line: str) -> None:
        self.stream.write("\r\033[2K" + line)
        self.stream.flush()
        self._painted = True

    def _erase(self) -> None:
        self.stream.write("\r\033[2K")
        self.stream.flush()
