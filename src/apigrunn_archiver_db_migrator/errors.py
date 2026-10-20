"""Exceptions raised by :mod:`apigrunn_archiver_db_migrator`.

Like apigrunn-archiver's own errors, each carries a stable ``code`` prefixed
onto its message. Give a new condition a new code rather than reusing one.

=====================  ===============================================
``MIGRATOR-E000``      :class:`MigratorError` (base, not raised)
``MIGRATOR-E100``      :class:`SourceError`
``MIGRATOR-E200``      :class:`OutputExistsError`
``MIGRATOR-E300``      :class:`VerificationError`
``MIGRATOR-E400``      :class:`UnsupportedArchiverError`
=====================  ===============================================

Whichever is raised, the source archive has not been modified.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path


class MigratorError(Exception):
    """Base class for every error raised by this package."""

    code = "MIGRATOR-E000"

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(f"[{self.code}] {message}")


class SourceError(MigratorError):
    """The source is missing, in use, or not an archive this tool can migrate."""

    code = "MIGRATOR-E100"


class OutputExistsError(MigratorError):
    """The output (or the backup of an in-place migration) already exists.

    Nothing is ever overwritten: move the file aside, or pick another path.
    """

    code = "MIGRATOR-E200"

    def __init__(self, path: Path, what: str = "output") -> None:
        self.path = path
        super().__init__(f"{what} {path} already exists; nothing was overwritten")


class VerificationError(MigratorError):
    """The migrated archive failed its checks, so it was thrown away."""

    code = "MIGRATOR-E300"

    def __init__(self, source: Path, problems: Sequence[str]) -> None:
        self.source = source
        self.problems = list(problems)
        listing = "".join(f"\n  - {p}" for p in self.problems)
        super().__init__(
            f"migrating {source} failed verification, so no output was written:{listing}"
        )


class UnsupportedArchiverError(MigratorError):
    """The installed apigrunn-archiver writes a schema this tool does not target."""

    code = "MIGRATOR-E400"

    def __init__(self, installed: str, schema: int, target: int) -> None:
        self.installed = installed
        self.schema = schema
        super().__init__(
            f"the installed apigrunn-archiver {installed} uses schema {schema}, but this "
            f"migrator only writes schema {target} (apigrunn-archiver 0.2.0)"
        )
