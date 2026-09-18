"""Generate the committed artifacts for the example schema library (W11).

For each vendored schema under ``examples/schemas/<name>/<name>.postgres.sql``
this script:

1. Parses the Postgres DDL through `foundation.ddl.parse_ddl` into a
   dialect-neutral `EntityGraph` (D6). ALTER-expressed UNIQUE/FOREIGN KEY
   constraints fold into the graph here -- see `foundation.ddl`'s module
   docstring ("ALTER-expressed constraints").
2. Renders that graph to SQLite DDL via `foundation.ddl.render_ddl` (D5: the
   only dialect this codebase executes).
3. Generates deterministic seed data (fixed RNG seed, no external source --
   upstream ships none) that respects every constraint actually present in
   the graph, including ones that look like upstream authoring mistakes
   (documented, not silently "fixed").
4. Writes four files per schema directory: the graph JSON, the rendered
   SQLite DDL, the seed INSERTs, and a NOTES.md capturing any parse warnings
   plus schema-specific commentary.
5. Self-checks every schema by creating an in-memory SQLite database from the
   rendered DDL and executing the seed script against it.

Run with:  uv run --project packages/foundation python examples/generate_examples.py

This script is committed for transparency/reproducibility, but per the task
brief the *authoritative* deliverable is its output -- the committed
graph.json / sqlite.sql / seed.sql files -- not a requirement to re-run this
at install or test time.
"""

from __future__ import annotations

import json
import random
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "foundation" / "src"))

from foundation.ddl import parse_ddl, render_ddl  # noqa: E402

EXAMPLES_DIR = Path(__file__).resolve().parent
SCHEMAS_DIR = EXAMPLES_DIR / "schemas"
SEED = 20260918  # deterministic: today's date at authoring time (memory/decisions.md convention)


# ---------------------------------------------------------------------------
# SQL literal formatting for the committed seed.sql files
# ---------------------------------------------------------------------------


def sql_lit(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int | float):
        return str(value)
    text = str(value)
    return "'" + text.replace("'", "''") + "'"


def insert_stmt(table: str, row: dict[str, object]) -> str:
    cols = ", ".join(f'"{c}"' for c in row)
    vals = ", ".join(sql_lit(v) for v in row.values())
    return f'INSERT INTO "{table}" ({cols}) VALUES ({vals});'


def render_seed(rows_by_table: dict[str, list[dict[str, object]]]) -> str:
    lines: list[str] = []
    for table, rows in rows_by_table.items():
        if not rows:
            continue
        lines.append(f"-- {table}: {len(rows)} rows")
        lines.extend(insert_stmt(table, row) for row in rows)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# small deterministic data helpers
# ---------------------------------------------------------------------------

_FIRST = [
    "ava",
    "noah",
    "mia",
    "leo",
    "zoe",
    "kai",
    "luna",
    "finn",
    "nora",
    "omar",
    "ines",
    "theo",
    "yara",
    "milo",
    "sana",
    "eli",
    "tara",
    "remy",
    "iris",
    "dev",
    "cleo",
    "arlo",
    "vera",
    "hugo",
    "maya",
    "silas",
    "nia",
    "oren",
    "wren",
    "ezra",
    "juno",
    "kian",
    "lior",
    "mabel",
    "nico",
    "opal",
    "priya",
    "quinn",
    "rhea",
    "soren",
]


def ts(base_year: int, base_month: int, base_day: int, offset_minutes: int) -> str:
    base = datetime(base_year, base_month, base_day, 8, 0, 0)
    return (base + timedelta(minutes=offset_minutes)).strftime("%Y-%m-%d %H:%M:%S")


def make_users(rng: random.Random, n: int, *, username: bool = False) -> list[dict[str, object]]:
    rows = []
    for i in range(1, n + 1):
        handle = f"{_FIRST[(i - 1) % len(_FIRST)]}{i}"
        row: dict[str, object] = {
            "id": i,
            "email": f"{handle}@example.test",
        }
        if username:
            row["username"] = handle
        row["password_digest"] = f"$2b$12$seed.digest.{i:04d}"
        row["created_at"] = ts(2023, 1, 1, i * 180)
        row["updated_at"] = row["created_at"]
        rows.append(row)
    return rows


def pick_unique_pairs(
    rng: random.Random, left: list[int], right: list[int], n: int
) -> list[tuple[int, int]]:
    """`n` distinct (left, right) pairs, order-preserving-ish, deterministic."""
    seen: set[tuple[int, int]] = set()
    out: list[tuple[int, int]] = []
    attempts = 0
    max_attempts = n * 50 + 200
    while len(out) < n and attempts < max_attempts:
        attempts += 1
        pair = (rng.choice(left), rng.choice(right))
        if pair in seen:
            continue
        seen.add(pair)
        out.append(pair)
    return out


LOREM = [
    "the",
    "quick",
    "fox",
    "jumps",
    "over",
    "lazy",
    "dogs",
    "while",
    "curious",
    "cats",
    "watch",
    "from",
    "the",
    "fence",
    "and",
    "a",
    "gentle",
    "rain",
    "begins",
    "to",
    "fall",
    "across",
    "the",
    "quiet",
    "town",
    "square",
]


def lorem(rng: random.Random, n_words: int) -> str:
    return " ".join(rng.choices(LOREM, k=n_words)).capitalize()


# ---------------------------------------------------------------------------
# per-schema seed generators
# ---------------------------------------------------------------------------


def gen_blog_with_tags(rng: random.Random) -> dict[str, list[dict[str, object]]]:
    users = make_users(rng, 25)
    articles = []
    for i in range(1, 41):
        author = rng.choice(users)
        articles.append(
            {
                "id": i,
                "author_id": author["id"],
                "title": lorem(rng, 5),
                "body": lorem(rng, 30),
                "created_at": ts(2023, 2, 1, i * 240),
                "updated_at": ts(2023, 2, 1, i * 240),
            }
        )
    tag_names = [
        "python",
        "sql",
        "databases",
        "webdev",
        "rust",
        "testing",
        "design",
        "career",
        "opinion",
        "tutorial",
        "news",
        "golang",
        "security",
        "ml",
        "frontend",
        "backend",
        "devops",
        "career-advice",
        "books",
        "meta",
    ]
    tags = [
        {
            "id": i + 1,
            "name": name,
            "created_at": ts(2023, 1, 15, i * 60),
            "updated_at": ts(2023, 1, 15, i * 60),
        }
        for i, name in enumerate(tag_names)
    ]
    # Every article gets 0-3 tags; article 1 is deliberately tag-less (zero children).
    taggings = []
    tid = 1
    for article in articles:
        if article["id"] == 1:
            continue
        k = rng.choice([0, 1, 1, 2, 2, 3])
        chosen = rng.sample(tags, k)
        for tag in chosen:
            taggings.append(
                {
                    "id": tid,
                    "article_id": article["id"],
                    "tag_id": tag["id"],
                    "created_at": ts(2023, 2, 2, tid * 30),
                    "updated_at": ts(2023, 2, 2, tid * 30),
                }
            )
            tid += 1
    return {"users": users, "articles": articles, "tags": tags, "taggings": taggings}


def gen_blog_with_likes(rng: random.Random) -> dict[str, list[dict[str, object]]]:
    users = make_users(rng, 25)
    articles = []
    for i in range(1, 31):
        author = rng.choice(users)
        articles.append(
            {
                "id": i,
                "author_id": author["id"],
                "title": lorem(rng, 5),
                "body": lorem(rng, 30),
                "created_at": ts(2023, 2, 1, i * 240),
                "updated_at": ts(2023, 2, 1, i * 240),
            }
        )
    # Force a tie at the top: articles 1 and 2 each get exactly 6 likes,
    # every other article gets fewer -- makes "most-liked article" ambiguous
    # on purpose (the boundary-tie edge case).
    likes = []
    lid = 1

    def add_likes(article_id: int, liker_ids: list[int]) -> None:
        nonlocal lid
        for uid in liker_ids:
            likes.append(
                {
                    "id": lid,
                    "article_id": article_id,
                    "user_id": uid,
                    "created_at": ts(2023, 2, 3, lid * 15),
                    "updated_at": ts(2023, 2, 3, lid * 15),
                }
            )
            lid += 1

    all_user_ids = [u["id"] for u in users]
    add_likes(1, all_user_ids[0:6])
    add_likes(2, all_user_ids[6:12])
    for article in articles[2:]:
        k = rng.choice([0, 1, 2, 3, 4, 5])
        likers = rng.sample(all_user_ids, k)
        add_likes(article["id"], likers)
    return {"users": users, "articles": articles, "likes": likes}


def gen_question_answer(rng: random.Random) -> dict[str, list[dict[str, object]]]:
    users = make_users(rng, 30, username=True)
    questions = []
    for i in range(1, 41):
        author = rng.choice(users)
        questions.append(
            {
                "id": i,
                "author_id": author["id"],
                "best_answer_id": None,  # patched below once answers exist
                "title": lorem(rng, 6) + "?",
                "body": lorem(rng, 25),
                "created_at": ts(2023, 3, 1, i * 200),
                "updated_at": ts(2023, 3, 1, i * 200),
            }
        )
    answers = []
    aid = 1
    # question 1 is deliberately unanswered (zero children).
    for q in questions:
        if q["id"] == 1:
            continue
        n_answers = rng.choice([1, 1, 2])
        answerers = rng.sample([u["id"] for u in users if u["id"] != q["author_id"]], n_answers)
        for uid in answerers:
            answers.append(
                {
                    "id": aid,
                    "author_id": uid,
                    "question_id": q["id"],
                    "body": lorem(rng, 20),
                    "created_at": ts(2023, 3, 2, aid * 90),
                    "updated_at": ts(2023, 3, 2, aid * 90),
                }
            )
            aid += 1
    answers_by_question: dict[int, list[dict[str, object]]] = {}
    for a in answers:
        answers_by_question.setdefault(a["question_id"], []).append(a)
    # Half of answered questions pick a best answer (the other half leaves
    # best_answer_id NULL -- the nullable-column edge case, exercising the
    # circular FK questions.best_answer_id <-> answers.question_id).
    for q in questions:
        candidates = answers_by_question.get(q["id"])
        if candidates and q["id"] % 2 == 0:
            q["best_answer_id"] = rng.choice(candidates)["id"]
    votes = []
    vid = 1
    for a in answers:
        voters = rng.sample(
            [u["id"] for u in users if u["id"] != a["author_id"]], rng.choice([0, 1, 1, 2])
        )
        for uid in voters:
            votes.append(
                {
                    "id": vid,
                    "answer_id": a["id"],
                    "voter_id": uid,
                    "score": rng.choice([-1, 1, 1]),
                    "created_at": ts(2023, 3, 3, vid * 20),
                    "updated_at": ts(2023, 3, 3, vid * 20),
                }
            )
            vid += 1
    return {"users": users, "questions": questions, "answers": answers, "answer_votes": votes}


def gen_surveys(rng: random.Random) -> dict[str, list[dict[str, object]]]:
    users = make_users(rng, 25)
    surveys = []
    for i in range(1, 23):
        surveys.append(
            {
                "id": i,
                "author_id": rng.choice(users)["id"],
                "title": lorem(rng, 5),
                "created_at": ts(2023, 4, 1, i * 500),
                "updated_at": ts(2023, 4, 1, i * 500),
            }
        )
    choices = []
    cid = 1
    choices_by_survey: dict[int, list[int]] = {}
    for s in surveys:
        n = rng.choice([2, 3, 3, 4])
        ids = []
        for _ in range(n):
            choices.append({"id": cid, "survey_id": s["id"], "description": lorem(rng, 3)})
            ids.append(cid)
            cid += 1
        choices_by_survey[s["id"]] = ids
    responses = []
    rid = 1
    response_choices = []
    rcid = 1
    for s in surveys:
        if s["id"] == 1:
            continue  # survey 1 has zero responses (zero children).
        n_resp = rng.choice([2, 3, 3, 4])
        respondents = rng.sample([u["id"] for u in users], n_resp)
        for uid in respondents:
            responses.append(
                {
                    "id": rid,
                    "survey_id": s["id"],
                    "respondent_id": uid,
                    "created_at": ts(2023, 4, 5, rid * 40),
                    "updated_at": ts(2023, 4, 5, rid * 40),
                }
            )
            survey_choice_ids = choices_by_survey[s["id"]]
            picked = rng.sample(survey_choice_ids, min(1, len(survey_choice_ids)))
            for choice_id in picked:
                response_choices.append(
                    {
                        "id": rcid,
                        "survey_choice_id": choice_id,
                        "survey_response_id": rid,
                        "created_at": ts(2023, 4, 5, rcid * 40 + 1),
                        "updated_at": ts(2023, 4, 5, rcid * 40 + 1),
                    }
                )
                rcid += 1
            rid += 1
    return {
        "users": users,
        "surveys": surveys,
        "survey_choices": choices,
        "survey_responses": responses,
        "survey_response_choices": response_choices,
    }


_ZERO_COMMENTS_SUBMISSION_ID = 3
_REPLY_PROBABILITY = 0.4


def gen_reddit(rng: random.Random) -> dict[str, list[dict[str, object]]]:
    users = make_users(rng, 30, username=True)
    submissions = []
    for i in range(1, 41):
        submissions.append(
            {
                "id": i,
                "author_id": rng.choice(users)["id"],
                "url": f"https://example.test/story/{i}",
                "created_at": ts(2023, 5, 1, i * 150),
                "updated_at": ts(2023, 5, 1, i * 150),
            }
        )
    sub_votes = []
    svid = 1
    # Force a scoring tie at the top between submissions 1 and 2.
    all_uids = [u["id"] for u in users]

    def add_votes(sub_id: int, voter_ids: list[int], score: int) -> None:
        nonlocal svid
        for uid in voter_ids:
            sub_votes.append(
                {
                    "id": svid,
                    "submission_id": sub_id,
                    "voter_id": uid,
                    "score": score,
                    "created_at": ts(2023, 5, 2, svid * 10),
                    "updated_at": ts(2023, 5, 2, svid * 10),
                }
            )
            svid += 1

    add_votes(1, all_uids[0:6], 1)
    add_votes(2, all_uids[6:12], 1)
    for sub in submissions[2:]:
        voters = rng.sample(all_uids, rng.choice([0, 1, 1, 2, 3]))
        for uid in voters:
            add_votes(sub["id"], [uid], rng.choice([-1, 1, 1]))

    comments: list[dict[str, object]] = []
    comment_id = 1
    # submission 3 has zero comments (zero children); the rest get 0-3
    # top-level comments each with a 0-1 nested reply (self-referencing FK,
    # and parent_id NULL for every top-level comment -- the nullable edge case).
    for sub in submissions:
        if sub["id"] == _ZERO_COMMENTS_SUBMISSION_ID:
            continue
        n_top = rng.choice([0, 1, 1, 2, 3])
        for _ in range(n_top):
            author = rng.choice(all_uids)
            comments.append(
                {
                    "id": comment_id,
                    "author_id": author,
                    "submission_id": sub["id"],
                    "parent_id": None,
                    "body": lorem(rng, 12),
                    "created_at": ts(2023, 5, 3, comment_id * 25),
                    "updated_at": ts(2023, 5, 3, comment_id * 25),
                }
            )
            top_id = comment_id
            comment_id += 1
            if rng.random() < _REPLY_PROBABILITY:
                reply_author = rng.choice(all_uids)
                comments.append(
                    {
                        "id": comment_id,
                        "author_id": reply_author,
                        "submission_id": sub["id"],
                        "parent_id": top_id,
                        "body": lorem(rng, 8),
                        "created_at": ts(2023, 5, 3, comment_id * 25),
                        "updated_at": ts(2023, 5, 3, comment_id * 25),
                    }
                )
                comment_id += 1

    comment_votes = []
    cvid = 1
    for c in comments:
        voters = rng.sample(all_uids, rng.choice([0, 0, 1, 2]))
        for uid in voters:
            comment_votes.append(
                {
                    "id": cvid,
                    "comment_id": c["id"],
                    "voter_id": uid,
                    "score": rng.choice([-1, 1]),
                    "created_at": ts(2023, 5, 4, cvid * 30),
                    "updated_at": ts(2023, 5, 4, cvid * 30),
                }
            )
            cvid += 1

    return {
        "users": users,
        "submissions": submissions,
        "submission_votes": sub_votes,
        "comments": comments,
        "comment_votes": comment_votes,
    }


def gen_photo_gallery(rng: random.Random) -> dict[str, list[dict[str, object]]]:
    users = make_users(rng, 20)
    albums = []
    for i in range(1, 31):
        albums.append(
            {
                "id": i,
                "user_id": rng.choice(users)["id"],
                "name": lorem(rng, 3),
                "created_at": ts(2023, 6, 1, i * 300),
                "updated_at": ts(2023, 6, 1, i * 300),
            }
        )
    photos = []
    pid = 1
    for album in albums:
        if album["id"] == 1:
            continue  # album 1 has zero photos (zero children).
        n = rng.choice([1, 2, 3, 4, 5])
        for _ in range(n):
            photos.append(
                {
                    "id": pid,
                    "album_id": album["id"],
                    "filename": f"img_{pid:04d}.jpg",
                    "created_at": ts(2023, 6, 2, pid * 20),
                    "updated_at": ts(2023, 6, 2, pid * 20),
                }
            )
            pid += 1
    return {"users": users, "albums": albums, "photos": photos}


_ZERO_TURNS_GAME_ID = 2


def gen_hangman(rng: random.Random) -> dict[str, list[dict[str, object]]]:
    users = make_users(rng, 22)
    phrase_texts = [
        "the early bird catches the worm",
        "better late than never",
        "practice makes perfect",
        "actions speak louder than words",
        "every cloud has a silver lining",
        "honesty is the best policy",
        "time heals all wounds",
        "knowledge is power",
        "curiosity killed the cat",
        "birds of a feather flock together",
        "the pen is mightier than the sword",
        "when in rome do as the romans do",
        "a picture is worth a thousand words",
        "absence makes the heart grow fonder",
        "beauty is in the eye of the beholder",
        "dont judge a book by its cover",
        "the grass is always greener",
        "two wrongs dont make a right",
        "you cant have your cake and eat it too",
        "where there is a will there is a way",
        "a watched pot never boils",
    ]
    phrases = [{"id": i + 1, "body": text} for i, text in enumerate(phrase_texts)]
    games = []
    for i in range(1, 41):
        started = ts(2023, 7, 1, i * 100)
        # roughly a third of games are still in progress -> completed_at NULL
        completed = None if i % 3 == 0 else ts(2023, 7, 1, i * 100 + rng.randint(5, 40))
        games.append(
            {
                "id": i,
                "player_id": rng.choice(users)["id"],
                "phrase_id": rng.choice(phrases)["id"],
                "guess_limit": rng.choice([6, 7, 8, 10]),
                "completed_at": completed,
                "created_at": started,
                "updated_at": completed or started,
            }
        )
    turns = []
    tid = 1
    letters = list("etaoinshrdlcumwfgypbvkjxqz")
    for game in games:
        if game["id"] == _ZERO_TURNS_GAME_ID:
            continue  # game 2 has zero turns (zero children -- just started).
        n_turns = rng.choice([1, 1, 2, 2, 3])
        chosen_letters = rng.sample(letters, n_turns)
        for letter in chosen_letters:
            turns.append(
                {
                    "id": tid,
                    "game_id": game["id"],
                    "letter_guessed": letter,
                    "created_at": ts(2023, 7, 1, game["id"] * 100 + tid),
                }
            )
            tid += 1
    return {"users": users, "phrases": phrases, "games": games, "turns": turns}


def gen_tic_tac_toe(rng: random.Random) -> dict[str, list[dict[str, object]]]:
    """See NOTES.md for `turns`: upstream's `ALTER TABLE turns ADD UNIQUE (game_id)`
    (present verbatim in the source, distinct from the earlier duplicated
    `UNIQUE (game_id, position)`) limits this table to at most one row per
    game. Seed data respects that constraint as written rather than silently
    dropping it."""
    users = make_users(rng, 24)
    games = []
    for i in range(1, 51):
        # A third of games are drawn (no winner) -- winner_id NULL is both
        # the nullable-column edge case and a natural "tie" boundary case.
        drawn = i % 3 == 0
        in_progress = i % 11 == 0
        winner = None if (drawn or in_progress) else None  # set below once players assigned
        games.append(
            {
                "id": i,
                "winner_id": winner,
                "completed_at": None if in_progress else ts(2023, 8, 1, i * 60 + 20),
                "created_at": ts(2023, 8, 1, i * 60),
                "updated_at": ts(2023, 8, 1, i * 60 + 20),
                "_drawn": drawn,
                "_in_progress": in_progress,
            }
        )
    all_uids = [u["id"] for u in users]
    games_players = []
    gpid = 1
    for game in games:
        p1, p2 = rng.sample(all_uids, 2)
        games_players.append({"id": gpid, "game_id": game["id"], "player_id": p1, "token": "X"})
        gpid += 1
        games_players.append({"id": gpid, "game_id": game["id"], "player_id": p2, "token": "O"})
        gpid += 1
        if not game["_drawn"] and not game["_in_progress"]:
            game["winner_id"] = rng.choice([p1, p2])
        del game["_drawn"]
        del game["_in_progress"]

    # turns: capped at exactly one row per game by upstream's own
    # ALTER TABLE turns ADD UNIQUE (game_id). Record the opening move for
    # most games; leave some games with zero recorded turns (zero children).
    turns = []
    tid = 1
    for game in games:
        if game["id"] % 5 == 0:
            continue  # ~20% of games: zero turns on record.
        gp_for_game = [gp for gp in games_players if gp["game_id"] == game["id"]]
        opener = gp_for_game[0]
        turns.append(
            {
                "id": tid,
                "game_id": game["id"],
                "player_id": opener["player_id"],
                "position": rng.randint(1, 9),
                "created_at": ts(2023, 8, 1, game["id"] * 60 + 1),
            }
        )
        tid += 1
    return {"users": users, "games": games, "games_players": games_players, "turns": turns}


GENERATORS = {
    "blog_with_tags": gen_blog_with_tags,
    "blog_with_likes": gen_blog_with_likes,
    "question_answer": gen_question_answer,
    "surveys": gen_surveys,
    "reddit": gen_reddit,
    "photo_gallery": gen_photo_gallery,
    "hangman": gen_hangman,
    "tic_tac_toe": gen_tic_tac_toe,
}

DESCRIPTIONS = {
    "blog_with_tags": "A basic blog where users publish articles and tag them with arbitrary tags.",
    "blog_with_likes": "A basic blog where users publish articles or like existing ones.",
    "question_answer": "A Q&A site a la Stack Overflow: users, questions, answers, and voting.",
    "surveys": "A survey site: users create surveys for other users to fill out.",
    "reddit": "A Reddit-like link aggregator: submissions, voting, and nested commenting.",
    "photo_gallery": "A basic photo gallery: users create albums and upload photos to them.",
    "hangman": "A player-vs-computer Hangman game: users, games, turns, and phrases.",
    "tic_tac_toe": "A collection of tic-tac-toe games: users, games, and turns.",
}


@dataclass
class GenResult:
    name: str
    table_count: int
    row_counts: dict[str, int]
    warnings: list[str]


def process_schema(name: str) -> GenResult:
    schema_dir = SCHEMAS_DIR / name
    source_path = schema_dir / f"{name}.postgres.sql"
    postgres_ddl = source_path.read_text()

    parse_result = parse_ddl(postgres_ddl, "postgres")
    graph = parse_result.graph
    sqlite_ddl = render_ddl(graph, "sqlite")

    # Seeded with a string (not a tuple): random.Random hashes str/bytes seeds
    # via a fixed SHA-512-based scheme, stable across interpreter runs even
    # with hash randomization enabled -- a tuple containing a str would hash
    # via the randomized `hash()` builtin and silently break reproducibility.
    rng = random.Random(f"{SEED}:{name}")
    rows_by_table = GENERATORS[name](rng)

    known_tables = {t.name for t in graph.tables}
    assert set(rows_by_table) <= known_tables, (name, set(rows_by_table) - known_tables)

    seed_sql = render_seed(rows_by_table)

    # Self-check: build a real in-memory SQLite DB from the rendered DDL and
    # load the generated seed into it. This is the same guarantee the
    # committed test suite re-checks later against the committed files.
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(sqlite_ddl)
        conn.executescript(seed_sql)
        conn.commit()
        row_counts = {}
        for table in graph.tables:
            (count,) = conn.execute(f'SELECT COUNT(*) FROM "{table.name}"').fetchone()
            row_counts[table.name] = count
    finally:
        conn.close()

    (schema_dir / "graph.json").write_text(
        json.dumps(json.loads(graph.model_dump_json()), indent=2, sort_keys=False) + "\n"
    )
    (schema_dir / "sqlite.sql").write_text(sqlite_ddl)
    (schema_dir / "seed.sql").write_text(seed_sql)

    notes_lines = [
        f"# {name} — ingestion notes\n",
        f"{DESCRIPTIONS[name]}\n",
        "## D6 pipeline\n",
        f"`{name}.postgres.sql` (vendored, verbatim) -> `foundation.ddl.parse_ddl` -> "
        "`graph.json` (dialect-neutral entity graph) -> `foundation.ddl.render_ddl` -> "
        "`sqlite.sql` (executed by D9's sample-database lifecycle).\n",
        "## parse_ddl warnings\n",
    ]
    if parse_result.warnings:
        notes_lines += [f"- {w}" for w in parse_result.warnings]
    else:
        notes_lines.append("(none)")
    notes_lines.append("")
    notes_lines.append("## row counts (seed.sql)\n")
    notes_lines += [f"- {t}: {c}" for t, c in row_counts.items()]
    (schema_dir / "NOTES.md").write_text("\n".join(notes_lines) + "\n")

    return GenResult(
        name=name,
        table_count=len(graph.tables),
        row_counts=row_counts,
        warnings=parse_result.warnings,
    )


def main() -> None:
    results = []
    for name in GENERATORS:
        results.append(process_schema(name))
    print(f"{'schema':22} {'tables':>7} {'total rows':>11}  warnings")
    for r in results:
        total = sum(r.row_counts.values())
        print(f"{r.name:22} {r.table_count:7d} {total:11d}  {len(r.warnings)}")


if __name__ == "__main__":
    main()
