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

__all__ = [
    "AFFIRMATIVE_EXAMPLES",
    "SCOPE_EXAMPLES",
    "Scope",
    "Verdict",
    "read",
    "read_scope",
]

Verdict = Literal["affirmative", "negative", "unrelated"]

#: The answer to "the model, its database, or both?" (W18). ``None`` is "that
#: was not an answer to my question", and is handled exactly like
#: ``"unrelated"`` above: the question is dropped, announced, and the utterance
#: is routed normally.
Scope = Literal["model", "database", "both"]


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


#: W18. Read in code for the same three reasons a "yes" is: a scope answer
#: decides how much gets deleted, a mislabelled enum must not be able to widen a
#: deletion, and the whole flow has to work with no model behind it.
#:
#: Checked in this order, and the order is the safety argument. "both" and "the
#: model" are checked before "the database", because every phrasing of the
#: larger answer ("the model and the database", "all of it") contains a cue for
#: the smaller one, and resolving that overlap the other way would silently
#: shrink what the user asked for -- after which they would confirm a
#: description that no longer matched their intent.
_SCOPE_PATTERNS: Final[tuple[tuple[Scope, tuple[re.Pattern[str], ...]], ...]] = (
    (
        # Checked before everything else because these say which one to KEEP,
        # and every one of them contains the cue for the other answer. "Keep the
        # model" read as "model" would delete the thing the sentence asked to
        # keep -- the worst misreading available in this function.
        "database",
        _compile(
            r"\b(keep|leave|preserve|save|hang on to) (the |my )?(data )?model\b",
            r"\bnot the (data )?model\b",
        ),
    ),
    (
        "both",
        _compile(
            r"^\s*both\b",
            r"\bboth (of )?(them|it|those)\b",
            r"\b(the )?(data )?model and (the |its )?(sample )?(database|db)\b",
            r"\b(the )?(sample )?(database|db) and (the |its )?(data )?model\b",
            r"^\s*(everything|all of it|the lot|the whole (lot|thing))\b",
            r"\bdelete everything\b",
        ),
    ),
    (
        "model",
        _compile(
            r"\bthe (whole|entire) (data )?model\b",
            r"^\s*(just |only )?the (data )?model\b",
            r"\b(data )?model,? (please|itself)\b",
            r"\b(just|only) the (data )?model\b",
            r"\bthe (design|whole thing)\b",
            r"\bthe (data )?model\b",
        ),
    ),
    (
        "database",
        _compile(
            r"^\s*(just |only )?the (sample )?(database|db|instance)\b",
            r"\b(just|only) the (sample )?(database|db|instance)\b",
            r"\b(sample )?(database|db|instance),? (please|only|itself)\b",
            r"\b(keep|leave) the (data )?model\b",
            r"\bthe (sample )?(database|db|instance)\b",
        ),
    ),
)

#: The last resort, and only when the two cues are **disjoint**. "the sports
#: league model" is a plain scope answer that matches none of the phrasings
#: above, because it names the thing instead of pointing at it.
#:
#: When both cue families appear and none of the explicit phrasings above fired,
#: the answer is ``None``: the sentence mentions a model and a database in some
#: arrangement we have no pattern for, and there is no safe way to rank them.
#: Re-asking costs a turn; picking the larger one costs a data model.
_MODEL_CUE: Final = re.compile(r"\b(data ?)?models?\b|\bdesigns?\b", re.IGNORECASE)
_DATABASE_CUE: Final = re.compile(r"\b(databases?|dbs?|instances?)\b", re.IGNORECASE)

#: Shown with the scope question, so the vocabulary is stated rather than guessed at.
SCOPE_EXAMPLES: Final = '"the model", "just the database", or "both"'


def read_scope(utterance: str) -> Scope | None:
    """Read one utterance as an answer to "the model, the database, or both?".

    Returns ``None`` for anything that is not clearly one of the three. A scope
    answer is not consent and never deletes anything on its own -- it produces
    the confirmation for that scope, which still has to be agreed to -- but it
    *does* choose how large the thing being described is, so the same
    "when in doubt, it was not an answer" rule applies.

    A question is never an answer, for the reason found by driving W17 live:
    "wait - which model?" is the user asking, and swallowing it as a choice
    would lose the question and pick a scope they never named.
    """
    text = utterance.replace("\u2019", "'").strip()
    if not text or _ASKS_SOMETHING.search(text):
        return None
    if any(p.search(text) for p in _NEGATIVE):
        return None
    for scope, patterns in _SCOPE_PATTERNS:
        if any(p.search(text) for p in patterns):
            return scope
    model_cue = bool(_MODEL_CUE.search(text))
    database_cue = bool(_DATABASE_CUE.search(text))
    if model_cue != database_cue:
        return "model" if model_cue else "database"
    return None


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
