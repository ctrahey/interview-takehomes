"""Recent conversation, reconstructed from the activity log, for the router (W17).

The second half of the defect Chris hit. ``RouterContext`` carried a checklist of
what *exists* plus the last question asked, and nothing about what was recently
*said* -- so "I already mentioned it - do you not have that context?" resolved to
nothing, three turns in a row, and the honest answer was that no, it did not.

**The history is read out of the D14 activity log, not kept in parallel.** The
log already records, per turn, the utterance (on ``router.classify``), the
intents it produced, and -- since W17 -- a ``subject``: the concrete object the
step touched. A second transcript store would be a second thing to keep
consistent with the first, and the log is already the thing that survives a
restart.

Three constraints shape everything below.

**Bounded, twice.** At most :data:`MAX_TURNS` turns and at most
:data:`CHAR_BUDGET` characters, whichever binds first, with the oldest dropped
when the budget does. A router prompt that grows with the conversation would
quietly turn a fixed-cost classification into one that gets slower and dearer
all session, and D16 keeps ``reasoning_effort="none"`` on that call precisely
because it is meant to stay cheap.

**Data, not instructions.** Everything here was typed by the user, and replaying
it verbatim into a prompt is a prompt-injection channel with a delay fuse: an
imperative that was correctly classified as data on turn 1 must not acquire
force by being quoted back on turn 4. So every line is flattened to one line,
truncated, and rendered inside a delimited block whose template says what it is
(``router.history``). The framing is the same one D13 requires for correctives
and D9 requires for schema text.

**No identifiers.** Subjects name things ("the sample database for data model
'bookstore'") and never carry a UUID. That is partly for the user's benefit and
mostly a hard requirement of D8: a fixture key is a hash of the prompt, so a
prompt containing a value that is freshly random per run is a prompt no captured
fixture can ever match again. Ids belong in the activity detail and on screen,
both of which we control; they must not reach the wire.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from t2s_nl.activity import ActivityRecord

__all__ = [
    "CHAR_BUDGET",
    "MAX_TURNS",
    "SCAN_ROWS",
    "RecentTurn",
    "recent_turns",
    "render",
]

#: How many previous turns the router sees. Five covers every referent case in
#: the transcripts (the furthest back "I already mentioned it" ever reached was
#: two) with room to spare, and stays small enough that the block is a handful
#: of short lines rather than a transcript.
MAX_TURNS = 5

#: Hard ceiling on the rendered block, in characters. Roughly 200 tokens. The
#: turn cap alone is not enough -- five pasted paragraphs are five turns.
CHAR_BUDGET = 700

#: Longest single utterance quoted. Long enough to carry a compound request,
#: short enough that one turn cannot eat the budget.
UTTERANCE_CHARS = 140

#: Longest subject label quoted.
SUBJECT_CHARS = 70

#: How many activity rows to read back over. A turn is 2 rows (router only) to
#: ~10 (a repaired query), so this comfortably contains MAX_TURNS turns without
#: reading a whole session's log on every utterance.
SCAN_ROWS = 120

#: The detail key a step sets to name the concrete object it touched. Read here
#: and written by ``t2s_nl.orchestrator``; it exists so that "it" in turn N+1 has
#: something to bind to when the user's own words in turn N were a pronoun.
SUBJECT_KEY = "subject"

_ROUTER_KIND = "router.classify"
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class RecentTurn:
    """One previous turn, as much of it as the router needs to resolve a pronoun."""

    utterance: str
    intents: tuple[str, ...] = ()
    subject: str | None = None

    def as_line(self) -> str:
        parts = [f'- user said: "{_clean(self.utterance, UTTERANCE_CHARS)}"']
        if self.intents:
            parts.append(f"understood as: {' → '.join(self.intents)}")
        if self.subject:
            parts.append(f"concerning: {_clean(self.subject, SUBJECT_CHARS)}")
        return " | ".join(parts)


def recent_turns(
    records: Iterable[ActivityRecord], *, limit: int = MAX_TURNS
) -> tuple[RecentTurn, ...]:
    """Group activity rows into turns, newest last.

    One ``router.classify`` pair opens a turn and everything up to the next one
    belongs to it -- exact, not heuristic, because a plan executes sequentially
    within a session and the log has a total order (``seq``).

    A trailing turn whose ``router.classify`` has not ended yet is dropped. That
    is the turn being routed *right now*: feeding an utterance to itself as
    context would be circular and, worse, would make the prompt depend on
    whether the caller happened to read the log before or after opening its own
    step.
    """
    turns: list[RecentTurn] = []
    closed: list[bool] = []
    for record in records:
        if record.kind == _ROUTER_KIND:
            if record.phase == "begin":
                turns.append(RecentTurn(utterance=_utterance_of(record)))
                closed.append(False)
            elif turns:
                turns[-1] = _with_intents(turns[-1], record)
                closed[-1] = True
            continue
        if turns and record.phase == "end":
            subject = _subject_of(record)
            if subject:
                turns[-1] = RecentTurn(turns[-1].utterance, turns[-1].intents, subject)

    if closed and not closed[-1]:
        turns.pop()
    return tuple(t for t in turns if t.utterance)[-limit:] if limit > 0 else ()


def render(turns: Sequence[RecentTurn], *, budget: int = CHAR_BUDGET) -> str:
    """The lines for the prompt block, oldest first, inside ``budget`` characters.

    Trimmed from the *oldest* end: the most recent turn is the one a pronoun
    almost always points at, so it is the last thing to go.
    """
    kept: list[str] = []
    used = 0
    for turn in reversed(turns):
        line = turn.as_line()
        if used + len(line) + 1 > budget:
            break
        kept.append(line)
        used += len(line) + 1
    return "\n".join(reversed(kept))


def _utterance_of(record: ActivityRecord) -> str:
    detail = record.detail or {}
    value = detail.get("utterance")
    return value if isinstance(value, str) else ""


def _with_intents(turn: RecentTurn, record: ActivityRecord) -> RecentTurn:
    detail = record.detail or {}
    raw = detail.get("directives")
    intents = tuple(str(i) for i in raw) if isinstance(raw, list) else ()
    return RecentTurn(turn.utterance, intents, turn.subject)


def _subject_of(record: ActivityRecord) -> str | None:
    detail = record.detail or {}
    value = detail.get(SUBJECT_KEY)
    return value if isinstance(value, str) and value.strip() else None


def _clean(text: str, limit: int) -> str:
    """One line, no control characters, no escape sequences, truncated.

    Flattening is a safety measure, not tidiness: a multi-line utterance that
    kept its newlines could draw a convincing fake section header inside the
    history block, and the block's whole defence is that the model can see where
    quoted user text begins and ends.
    """
    flat = _WHITESPACE.sub(" ", _CONTROL.sub(" ", text)).strip().replace('"', "'")
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"
