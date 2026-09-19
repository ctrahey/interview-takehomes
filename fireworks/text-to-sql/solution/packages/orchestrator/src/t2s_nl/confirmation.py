"""Reading a "yes" to a destructive question, in code, with no model involved (W17).

``destroy`` and ``clear_data`` are the first things layer 3 can do that cannot
be undone, so they are gated: the orchestrator describes precisely what would be
lost, records a :class:`foundation.models.PendingAction`, and does nothing. Only
an affirmative on the *next* turn executes it.

Three design commitments, each of which is a property something else depends on:

1. **The answer is read deterministically, never classified.** "yes" is not an
   intent-classification problem, and making it one would mean a destructive
   action could fire because a model mislabelled a sentence, or fail to fire
   because the network did. It would also make confirmation impossible offline,
   where a keyword router is deliberately forbidden from doing anything
   irreversible on a guess. So the whole vocabulary is here, as patterns.

2. **Anything that is not clearly yes or no is "moved on".** This is the
   conservative reading and it is the important one: a stale confirmation must
   not be answered by a sentence that was about something else. :data:`Verdict`
   has no fourth value; ambiguity resolves to ``"unrelated"``, which the
   orchestrator turns into "nothing was destroyed" plus ordinary routing of the
   new utterance.

3. **Affirmatives must be answers, not imperatives.** "yes" and "go ahead"
   confirm; "delete the bookstore database too" does not, even though it
   contains a destructive verb, because it names a *different* subject. A
   pattern here that matched general destructive language would turn the second
   destruction request in a row into an unconfirmed execution of the first.
"""

from __future__ import annotations

import re
from typing import Final, Literal

__all__ = ["AFFIRMATIVE_EXAMPLES", "Verdict", "read"]

Verdict = Literal["affirmative", "negative", "unrelated"]


def _compile(*patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p, re.IGNORECASE) for p in patterns)


#: Checked FIRST, so "no, don't" beats the "do" inside it and "not yet" never
#: reads as "yes". Refusal winning ties is the only safe precedence here.
#:
#: The soft cues are anchored to the *whole* utterance. "wait" on its own is a
#: refusal; "wait - what's my current schema?" is a new request that happens to
#: start with it, and reading the second as a refusal swallowed the question --
#: found by driving it, not by reasoning about it.
_NEGATIVE: Final = _compile(
    r"^\s*(no|nope|nah|n)\b",
    r"\b(don'?t|do not)\b",
    r"\b(cancel|abort|stop|forget it|never ?mind|nevermind|leave it|hold off)\b",
    # "not yet" / "not now" are refusals wherever they appear -- "yes but not
    # now" has to land on no. Only the bare-interjection cues are anchored to
    # the whole utterance, because those are the ones that also open sentences
    # that are not refusals at all.
    r"\b(not yet|not now|maybe later|some other time)\b",
    r"^\s*(wait|hold on|hang on)\s*[.!]*\s*$",
    r"\bchanged my mind\b",
    r"\bon second thought\b",
)

#: An utterance carrying a question is a question, whatever else is in it. It
#: cannot be a yes or a no, because the user is asking rather than answering --
#: and "unrelated" is not a dead end: it drops the pending action (announced)
#: and routes the question normally, which is what they wanted.
_ASKS_SOMETHING: Final = re.compile(r"\?")

#: Deliberately narrow and anchored. Every one of these is a *reply* to a
#: yes/no question rather than a fresh instruction -- see commitment 3 in the
#: module docstring.
_AFFIRMATIVE: Final = _compile(
    r"^\s*(yes|yep|yeah|yup|yes please|ok|okay|sure|affirmative|confirm(ed)?|y)\b",
    r"^\s*(do it|go ahead|proceed|confirm|please do|go for it|send it)\b",
    r"^\s*(delete|destroy|remove|drop|clear|wipe|nuke) (it|them|that|this)\s*[.!]?\s*$",
    r"^\s*(yes|yeah|yep|ok|okay|sure)\b.*\b(do it|delete|destroy|remove|drop|clear|go ahead)\b",
    r"^\s*(i'?m sure|i am sure|definitely|absolutely)\b",
    r"^\s*that'?s (right|correct)\b",
)

#: Shown in the confirmation prompt, so the user is told exactly what will work
#: rather than having to guess at our vocabulary.
AFFIRMATIVE_EXAMPLES: Final = '"yes", "do it", or "delete it"'


def read(utterance: str) -> Verdict:
    """Classify one utterance as an answer to a pending yes/no question.

    Never raises, never calls out, and never returns ``"affirmative"`` for
    anything it is not sure about -- the asymmetry is the point.
    """
    text = utterance.replace("’", "'").strip()
    if not text:
        return "unrelated"
    if _ASKS_SOMETHING.search(text):
        return "unrelated"
    if any(p.search(text) for p in _NEGATIVE):
        return "negative"
    if any(p.search(text) for p in _AFFIRMATIVE):
        return "affirmative"
    return "unrelated"
