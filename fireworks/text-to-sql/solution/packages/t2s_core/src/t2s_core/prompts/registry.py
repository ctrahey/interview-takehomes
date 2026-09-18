"""Versioned prompt templates with a registry (design §3).

Prompts are files, never inline string literals, for three reasons: they are
diffable, they are versioned independently of the code that uses them, and the
version that produced a result is recorded in ``ResultMetadata.prompt_versions``
so an eval report can attribute a score to a prompt.

File naming: ``<name>.<version>.md`` where name is dotted (``query.system``) and
version sorts lexicographically (``v1``, ``v2``...). ``PINS`` selects the version
in use; an unpinned name resolves to the highest available.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from string import Template

__all__ = ["PINS", "PromptRegistry", "PromptTemplate", "REGISTRY"]

TEMPLATE_DIR = Path(__file__).parent / "templates"

#: Explicit version pins. Adding a v2 file does not change behaviour until it is
#: pinned here -- prompt rollout is a deliberate act.
PINS: dict[str, str] = {
    "query.system": "v1",
    "query.user": "v1",
    "schema.system": "v1",
    "schema.user": "v1",
    "common.repair": "v1",
    "common.session_context": "v1",
}


@dataclass(frozen=True)
class PromptTemplate:  # not slots=True: cached_property needs __dict__
    name: str
    version: str
    path: Path

    @cached_property
    def text(self) -> str:
        return self.path.read_text(encoding="utf-8")

    @cached_property
    def placeholders(self) -> frozenset[str]:
        return frozenset(
            m.group("named") or m.group("braced")
            for m in Template.pattern.finditer(self.text)
            if m.group("named") or m.group("braced")
        )

    def render(self, **context: str) -> str:
        """Strict substitution: a missing or misspelled placeholder raises."""
        return Template(self.text).substitute(context)


class PromptRegistry:
    def __init__(self, directory: Path | None = None, pins: dict[str, str] | None = None):
        self.directory = directory or TEMPLATE_DIR
        self.pins = dict(PINS if pins is None else pins)
        self._templates: dict[tuple[str, str], PromptTemplate] = {}
        for path in sorted(self.directory.glob("*.md")):
            stem = path.stem  # e.g. "query.system.v1"
            name, _, version = stem.rpartition(".")
            if not name or not version:
                continue
            self._templates[(name, version)] = PromptTemplate(name, version, path)

    def versions(self, name: str) -> list[str]:
        return sorted(v for (n, v) in self._templates if n == name)

    def get(self, name: str, version: str | None = None) -> PromptTemplate:
        version = version or self.pins.get(name) or (self.versions(name) or [""])[-1]
        try:
            return self._templates[(name, version)]
        except KeyError:
            raise KeyError(f"no prompt template {name!r} version {version!r}") from None

    def render(self, name: str, /, version: str | None = None, **context: str) -> str:
        return self.get(name, version).render(**context)

    def pinned_versions(self, *names: str) -> dict[str, str]:
        """``{name: version}`` for the templates used in one generation, for metadata."""
        return {name: self.get(name).version for name in names}

    def names(self) -> list[str]:
        return sorted({n for (n, _) in self._templates})


REGISTRY = PromptRegistry()
