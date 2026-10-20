"""Migrate an apigrunn-archiver 0.1.0 archive to the format 0.2.0 reads.

    >>> from apigrunn_archiver_db_migrator import migrate
    >>> result = migrate("archive.db", "archive-v3.db")

apigrunn-archiver 0.2.0 has no migrations and refuses older archives. This
package rebuilds one from the HTML 0.1.0 stored, using 0.2.0's own schema and
parser, verifies it, and only then moves it into place.
"""

from ._version import __version__
from .errors import (
    MigratorError,
    OutputExistsError,
    SourceError,
    UnsupportedArchiverError,
    VerificationError,
)
from .migration import (
    SOURCE_SCHEMAS,
    TARGET_SCHEMA,
    MigrationResult,
    backup_path,
    dry_run,
    migrate,
    migrate_in_place,
    source_schema,
)

__all__ = [
    "SOURCE_SCHEMAS",
    "TARGET_SCHEMA",
    "MigrationResult",
    "MigratorError",
    "OutputExistsError",
    "SourceError",
    "UnsupportedArchiverError",
    "VerificationError",
    "__version__",
    "backup_path",
    "dry_run",
    "migrate",
    "migrate_in_place",
    "source_schema",
]
