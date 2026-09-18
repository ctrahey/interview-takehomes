"""Managed-directory path derivation for sample databases (D9).

D9: "Sample databases are per-database files under a managed directory; no
path supplied by a client ever reaches the filesystem." This module is the
single place that turns a `Database.id` into a filesystem path. Nothing else
in this package (or outside it) should construct a sample-database path by
hand.

The mechanism, not just the intent: `database_path` takes a `uuid.UUID` --
not a string -- so a caller cannot smuggle `../../etc/passwd` or a null byte
through by construction (there is no string parameter for a filename to hide
in). The filename is always `str(uuid.UUID(...))` re-stringified through the
`uuid` module's own canonical form, formatted with `uuid4.hex`, which is
guaranteed to be exactly 32 lowercase hex characters -- no separators, no
path separators, nothing else representable.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

_DEFAULT_ENV_VAR = "T2S_SAMPLE_DB_DIR"
_DEFAULT_DIR = Path.home() / ".t2s" / "sample_dbs"


def managed_directory() -> Path:
    """The directory sample database files live under.

    Overridable via the `T2S_SAMPLE_DB_DIR` environment variable (tests use
    this to point at a temp directory); otherwise `~/.t2s/sample_dbs`.
    """
    raw = os.environ.get(_DEFAULT_ENV_VAR)
    directory = Path(raw) if raw else _DEFAULT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def database_path(database_id: uuid.UUID) -> Path:
    """The filesystem path for a sample database, derived ONLY from its UUID.

    Takes a `uuid.UUID`, not a string, by design: there is no code path by
    which arbitrary client-supplied text can become part of the filename.
    """
    if not isinstance(database_id, uuid.UUID):
        raise TypeError(f"database_id must be a uuid.UUID, got {type(database_id).__name__}")
    filename = f"{database_id.hex}.sqlite3"
    path = managed_directory() / filename
    # Belt and suspenders: verify the resolved path never escapes the managed
    # directory, even though the construction above makes that unreachable.
    resolved_dir = managed_directory().resolve()
    resolved_path = path.resolve()
    if resolved_dir not in resolved_path.parents and resolved_path.parent != resolved_dir:
        raise RuntimeError("computed sample database path escaped the managed directory")
    return path
