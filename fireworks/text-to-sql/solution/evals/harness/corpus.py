"""Load ``evals/corpus`` — the manifest plus each schema's DDL and seed.

Read-only by contract: W4 consumes the corpus, it never edits it. The two
manifest fields added by W2 (``allowed_response_classes`` and
``forbid_ddl_dml``) are surfaced here as first-class attributes because the
scorer contract in design.md's addendum is written in terms of them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

__all__ = ["Corpus", "CorpusItem", "SchemaFixture", "load_corpus"]

CORPUS_DIR = Path(__file__).resolve().parent.parent / "corpus"
MANIFEST_PATH = CORPUS_DIR / "manifest.json"
SCHEMAS_DIR = CORPUS_DIR / "schemas"

SUPPORTED_FORMAT_VERSION = 1
ADVERSARIAL_TIER = "adversarial"


@dataclass(frozen=True, slots=True)
class SchemaFixture:
    """One eval schema: the DDL handed to the model, and the seed rows the
    scorer executes against."""

    name: str
    ddl: str
    seed: str


@dataclass(frozen=True, slots=True)
class CorpusItem:
    id: str
    schema: str
    tier: str
    question: str
    gold_sql: str | None
    expected_response_class: str
    allowed_response_classes: tuple[str, ...]
    forbid_ddl_dml: bool
    rationale: str

    @property
    def is_adversarial(self) -> bool:
        return self.tier == ADVERSARIAL_TIER

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> CorpusItem:
        allowed = raw.get("allowed_response_classes")
        # `null` means "exactly the expected class"; a list means set membership.
        classes = tuple(allowed) if allowed else (raw["expected_response_class"],)
        return cls(
            id=raw["id"],
            schema=raw["schema"],
            tier=raw["tier"],
            question=raw["question"],
            gold_sql=raw["gold_sql"],
            expected_response_class=raw["expected_response_class"],
            allowed_response_classes=classes,
            forbid_ddl_dml=bool(raw["forbid_ddl_dml"]),
            rationale=raw["rationale"],
        )


@dataclass(frozen=True, slots=True)
class Corpus:
    format_version: int
    items: tuple[CorpusItem, ...]
    schemas: dict[str, SchemaFixture]

    def ddl_for(self, item: CorpusItem) -> str:
        return self.schemas[item.schema].ddl

    def select(
        self,
        *,
        limit: int | None = None,
        tiers: frozenset[str] | None = None,
        ids: frozenset[str] | None = None,
    ) -> tuple[CorpusItem, ...]:
        """Subset the corpus for a cheap partial run. ``limit`` keeps manifest
        order so a truncated run is reproducible, and every caller of this
        records the selection in the report (no silent drops)."""
        chosen = [
            item
            for item in self.items
            if (tiers is None or item.tier in tiers) and (ids is None or item.id in ids)
        ]
        return tuple(chosen[:limit] if limit is not None else chosen)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def load_corpus(manifest_path: Path = MANIFEST_PATH) -> Corpus:
    raw = json.loads(_read(manifest_path))
    version = int(raw["format_version"])
    if version != SUPPORTED_FORMAT_VERSION:
        raise ValueError(
            f"corpus manifest format_version {version} is not supported by this harness "
            f"(expected {SUPPORTED_FORMAT_VERSION}); update evals/harness before running."
        )
    items = tuple(CorpusItem.from_raw(entry) for entry in raw["items"])
    names = sorted({item.schema for item in items})
    schemas = {
        name: SchemaFixture(
            name=name,
            ddl=_read(SCHEMAS_DIR / name / "ddl.sql"),
            seed=_read(SCHEMAS_DIR / name / "seed.sql"),
        )
        for name in names
    }
    return Corpus(format_version=version, items=items, schemas=schemas)
