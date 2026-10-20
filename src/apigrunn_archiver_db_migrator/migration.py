"""Migrate an apigrunn-archiver 0.1.0 archive to the 0.2.0 format.

0.1.0 wrote schema 1 (runs, blobs, snapshots) or, from its talk index
onwards, schema 2 (plus parsed_pages and talk_cards). 0.2.0 reads only
schema 3 and has no migrations of its own. Everything schema 3 holds is
either copied as is or derived from the stored HTML, which 0.1.0 kept for
every response it received:

- ``runs`` and ``blobs`` are copied unchanged, ``tool_version`` included.
- ``snapshots`` lose ``changed``, which 0.2.0 computes (by talks, not
  bytes), and gain ``body_sha256``. 0.1.0 stored every body it received, so
  that is the snapshot's own blob and every migrated snapshot is exact.
- Error responses keep their status, error and body hash but no longer
  point at a body, as in 0.2.0. Bodies only they returned are kept unless
  asked to drop them; nothing in 0.2.0 shows them.
- The talk index (``talk_versions``, ``page_versions``, ``page_talks``,
  ``blob_parses``) is not converted but rebuilt by 0.2.0's own parser, so
  every hash in it is computed by the code that will check it. A schema 2
  index is only compared against the result, as a cross-check.

The schema is created by the installed apigrunn-archiver itself rather than
copied from it. The source is only ever opened read-only, and the migrated
archive is built in a temporary file next to the output and verified before
it is moved into place, so a failure at any point leaves no partial output.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

import apigrunn_archiver
from apigrunn_archiver import PARSER_VERSION, SCHEMA_VERSION, IntegrityError, Store
from apigrunn_archiver.index import ensure_index

from .errors import OutputExistsError, SourceError, UnsupportedArchiverError, VerificationError

logger = logging.getLogger(__name__)

#: Schemas written by apigrunn-archiver 0.1.0.
SOURCE_SCHEMAS = (1, 2)
#: The schema written by apigrunn-archiver 0.2.0, the only one this tool produces.
TARGET_SCHEMA = 3

_REQUIRED_TABLES = {
    1: {"runs", "blobs", "snapshots"},
    2: {"runs", "blobs", "snapshots", "parsed_pages", "talk_cards"},
}

_SNAPSHOT_COLUMNS = (
    "run_id", "url", "final_url", "fetched_at", "status_code", "error",
    "headers", "etag", "last_modified",
)

# Blobs that only error responses returned. 0.2.0 stores no body for those.
_ERROR_ONLY_BLOBS = """
    SELECT blob_sha256 FROM snapshots WHERE error IS NOT NULL AND blob_sha256 IS NOT NULL
    EXCEPT
    SELECT blob_sha256 FROM snapshots WHERE error IS NULL AND blob_sha256 IS NOT NULL
"""

# A talk card as both indexes describe it, in the same order.
_CARD_FIELDS = ("video_id", "title", "speaker", "description", "talk_url", "year", "thumbnail_url")


@dataclass(frozen=True, slots=True)
class MigrationResult:
    """What a migration did, or with ``output=None``, would do."""

    source: Path
    source_schema: int
    #: The apigrunn-archiver versions that wrote the source's runs.
    tool_versions: tuple[str, ...]
    output: Path | None
    #: The original archive, kept by an in-place migration.
    backup: Path | None
    runs: int
    snapshots: int
    #: Blobs in the migrated archive.
    blobs: int
    #: Blobs that only error responses returned, whether kept or dropped.
    error_bodies: int
    error_bodies_dropped: bool
    page_versions: int
    talk_versions: int
    #: ``(url, blob_sha256, message)`` for pages 0.2.0's parser could not read.
    #: They are migrated as unparseable pages, HTML included.
    parse_errors: tuple[tuple[str, str, str], ...]
    #: ``(url, blob_sha256, reason)`` where the rebuilt index disagrees with the
    #: schema 2 index of the source. A warning: the rebuilt one is what 0.2.0 uses.
    index_mismatches: tuple[tuple[str, str, str], ...]


def check_archiver() -> None:
    """Refuse to run against an apigrunn-archiver that does not write schema 3."""
    if SCHEMA_VERSION != TARGET_SCHEMA:
        raise UnsupportedArchiverError(apigrunn_archiver.__version__, SCHEMA_VERSION, TARGET_SCHEMA)


def backup_path(source: str | os.PathLike[str], schema: int) -> Path:
    """Where an in-place migration keeps the original: ``archive.db.schema2.bak``."""
    source = Path(source)
    return source.with_name(f"{source.name}.schema{schema}.bak")


def source_schema(source: str | os.PathLike[str]) -> int:
    """The schema of ``source``, after checking it is a 0.1.0 archive not in use."""
    source = Path(source)
    if not source.exists():
        raise SourceError(f"{source} does not exist")
    if not source.is_file():
        raise SourceError(f"{source} is not a file")
    for suffix in ("-wal", "-journal"):
        sidecar = Path(f"{source}{suffix}")
        if sidecar.exists():
            raise SourceError(
                f"{sidecar} exists, so something may still be writing to this archive. "
                f"Close it and try again; a log left behind by a crash is folded back in "
                f"by opening the archive once with apigrunn-archiver"
            )
    try:
        with _read_only(source) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )}
    except sqlite3.DatabaseError as exc:
        raise SourceError(f"{source} is not an SQLite database: {exc}") from exc

    if version == TARGET_SCHEMA:
        raise SourceError(f"{source} is already at schema {TARGET_SCHEMA}; nothing to migrate")
    if version not in SOURCE_SCHEMAS:
        what = "an empty database" if not tables else f"at schema {version}"
        raise SourceError(
            f"{source} is {what}, not an apigrunn-archiver 0.1.0 archive "
            f"(schema {' or '.join(map(str, SOURCE_SCHEMAS))})"
        )
    missing = _REQUIRED_TABLES[version] - tables
    if missing:
        raise SourceError(
            f"{source} claims schema {version} but has no {', '.join(sorted(missing))} table"
        )
    return version


def migrate(
    source: str | os.PathLike[str],
    output: str | os.PathLike[str],
    *,
    drop_error_bodies: bool = False,
) -> MigrationResult:
    """Write a schema 3 copy of ``source`` to ``output``, which must not exist."""
    source, output = Path(source), Path(output)
    check_archiver()
    schema = source_schema(source)
    if output.exists():
        raise OutputExistsError(output)

    tmp, result = _build(source, schema, output.parent, drop_error_bodies=drop_error_bodies)
    try:
        _link(tmp, output, "output")
    finally:
        _remove(tmp)
    return replace(result, output=output)


def migrate_in_place(
    source: str | os.PathLike[str], *, drop_error_bodies: bool = False
) -> MigrationResult:
    """Replace ``source`` with its schema 3 copy, keeping the original as a backup.

    The backup is the original file itself (hard-linked where possible), and
    the new archive takes its place in one rename, so ``source`` always names
    a complete archive.
    """
    source = Path(source)
    check_archiver()
    schema = source_schema(source)
    backup = backup_path(source, schema)
    if backup.exists():
        raise OutputExistsError(backup, "backup")

    tmp, result = _build(source, schema, source.parent, drop_error_bodies=drop_error_bodies)
    try:
        _link(source, backup, "backup")
        os.replace(tmp, source)
    finally:
        _remove(tmp)
    return replace(result, output=source, backup=backup)


def dry_run(source: str | os.PathLike[str], *, drop_error_bodies: bool = False) -> MigrationResult:
    """Migrate and verify into a temporary file, then throw it away."""
    source = Path(source)
    check_archiver()
    schema = source_schema(source)
    with tempfile.TemporaryDirectory(prefix="apigrunn-archiver-db-migrator-") as directory:
        tmp, result = _build(source, schema, Path(directory), drop_error_bodies=drop_error_bodies)
        _remove(tmp)
    return result


# ---------------------------------------------------------------- internals


def _build(
    source: Path, schema: int, directory: Path, *, drop_error_bodies: bool
) -> tuple[Path, MigrationResult]:
    """Build and verify the migrated archive in a new temporary file in ``directory``.

    Returns the file, which the caller moves into place or removes. On any
    failure the file is removed before the exception propagates.
    """
    fd, name = tempfile.mkstemp(prefix=f".{source.name}.", suffix=".migrating", dir=directory)
    os.close(fd)
    tmp = Path(name)
    try:
        # An empty file is an empty database: Store creates the 0.2.0 schema in it.
        Store(tmp).close()
        with _read_only(source) as src, closing(sqlite3.connect(tmp)) as dst:
            error_only = _copy(source, src, dst, drop_error_bodies=drop_error_bodies)

        store = Store(tmp)
        try:
            index = ensure_index(store)
            with _read_only(source) as src, closing(sqlite3.connect(tmp)) as out:
                problems = _verify(src, out, store, error_only, drop_error_bodies)
                mismatches = _compare_index(src, out, store) if schema >= 2 else []
                stats = {
                    table: out.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("runs", "snapshots", "blobs", "page_versions", "talk_versions")
                }
                tool_versions = tuple(
                    row[0] for row in src.execute(
                        "SELECT DISTINCT tool_version FROM runs ORDER BY tool_version"
                    )
                )
        finally:
            store.close()

        # Closing the last connection checkpoints the log into the file. If
        # that did not happen, moving the file alone would lose data.
        if Path(f"{tmp}-wal").exists():
            problems.append("the migrated archive's write-ahead log was not checkpointed")
        if problems:
            raise VerificationError(source, problems)
    except BaseException:
        _remove(tmp)
        raise

    return tmp, MigrationResult(
        source=source,
        source_schema=schema,
        tool_versions=tool_versions,
        output=None,
        backup=None,
        runs=stats["runs"],
        snapshots=stats["snapshots"],
        blobs=stats["blobs"],
        error_bodies=len(error_only),
        error_bodies_dropped=drop_error_bodies,
        page_versions=stats["page_versions"],
        talk_versions=stats["talk_versions"],
        parse_errors=tuple(index.errors),
        index_mismatches=tuple(mismatches),
    )


def _copy(
    source: Path, src: sqlite3.Connection, dst: sqlite3.Connection, *, drop_error_bodies: bool
) -> set[str]:
    """Copy runs, blobs and snapshots. Returns the blobs only error responses returned."""
    error_only = {row[0] for row in src.execute(_ERROR_ONLY_BLOBS)}
    skip = error_only if drop_error_bodies else set()
    columns = ", ".join(_SNAPSHOT_COLUMNS)

    dst.execute("PRAGMA foreign_keys = ON")
    try:
        with dst:
            dst.executemany(
                "INSERT INTO runs (id, started_at, finished_at, tool_version, status) "
                "VALUES (?, ?, ?, ?, ?)",
                map(tuple, src.execute(
                    "SELECT id, started_at, finished_at, tool_version, status FROM runs ORDER BY id"
                )),
            )
            dst.executemany(
                "INSERT INTO blobs (sha256, size, compression, data) VALUES (?, ?, ?, ?)",
                (
                    tuple(row)
                    for row in src.execute("SELECT sha256, size, compression, data FROM blobs")
                    if row[0] not in skip
                ),
            )
            dst.executemany(
                f"INSERT INTO snapshots ({columns}, body_sha256, blob_sha256) "
                f"VALUES ({', '.join('?' * len(_SNAPSHOT_COLUMNS))}, ?, ?)",
                map(tuple, src.execute(
                    f"""
                    SELECT {columns}, blob_sha256,
                           CASE WHEN error IS NULL THEN blob_sha256 END
                    FROM snapshots ORDER BY run_id, url
                    """
                )),
            )
    except sqlite3.IntegrityError as exc:
        raise SourceError(
            f"{source} is inconsistent ({exc}); check it with the apigrunn-archiver "
            f"0.1.0 'verify' command"
        ) from exc
    return error_only


def _verify(
    src: sqlite3.Connection,
    out: sqlite3.Connection,
    store: Store,
    error_only: set[str],
    drop_error_bodies: bool,
) -> list[str]:
    """Check the migrated archive the way 0.2.0 sees it, and against the source."""
    problems: list[str] = []

    version = store.schema_version
    if version != TARGET_SCHEMA:
        problems.append(f"schema version is {version}, not {TARGET_SCHEMA}")
    problems += [f"sqlite: {message}" for message in store.integrity_check()]

    for sha in store.blob_shas():
        try:
            store.blob(sha)
        except IntegrityError as exc:
            problems.append(exc.message)
    problems += [
        f"run {run_id} {url} points at missing blob {sha}"
        for run_id, url, sha in store.dangling_blob_refs()
    ]
    problems += [f"index {sha}: {reason}" for sha, reason in store.bad_index_rows()]
    problems += [
        f"{url} ({sha[:12]}) was not parsed" for sha, url, _ in store.unparsed_blobs(PARSER_VERSION)
    ]

    runs_sql = "SELECT id, started_at, finished_at, tool_version, status FROM runs ORDER BY id"
    if [tuple(r) for r in src.execute(runs_sql)] != [tuple(r) for r in out.execute(runs_sql)]:
        problems.append("runs differ from the source")

    columns = ", ".join(_SNAPSHOT_COLUMNS)
    expected = {
        tuple(row)
        for row in src.execute(
            f"SELECT {columns}, blob_sha256, CASE WHEN error IS NULL THEN blob_sha256 END "
            f"FROM snapshots"
        )
    }
    found = {
        tuple(row) for row in out.execute(f"SELECT {columns}, body_sha256, blob_sha256 FROM snapshots")
    }
    if expected != found:
        problems.append(
            f"snapshots differ from the source: {len(expected - found)} missing or changed, "
            f"{len(found - expected)} unexpected"
        )

    wanted = {row[0] for row in src.execute("SELECT sha256 FROM blobs")}
    if drop_error_bodies:
        wanted -= error_only
    have = {row[0] for row in out.execute("SELECT sha256 FROM blobs")}
    if wanted != have:
        problems.append(
            f"blobs differ from the source: {len(wanted - have)} missing, "
            f"{len(have - wanted)} unexpected"
        )
    return problems


def _compare_index(
    src: sqlite3.Connection, out: sqlite3.Connection, store: Store
) -> list[tuple[str, str, str]]:
    """Where the rebuilt talk index disagrees with the source's schema 2 index.

    Only pages both indexes cover are compared: 0.1.0 may not have indexed
    every page, and the rebuilt index leaves out dropped error bodies.
    """
    fields = ", ".join(_CARD_FIELDS)
    rebuilt = {
        (row[0], row[1]): (row[2], row[3])
        for row in out.execute("SELECT blob_sha256, url, page_sha256, error FROM blob_parses")
    }
    mismatches = []
    for sha, url, old_error in src.execute(
        "SELECT blob_sha256, url, error FROM parsed_pages ORDER BY url, blob_sha256"
    ):
        if (sha, url) not in rebuilt:
            continue
        page, new_error = rebuilt[(sha, url)]
        if old_error is not None and new_error is not None:
            continue
        if old_error is not None:
            mismatches.append((url, sha, "0.1.0 could not parse this page, 0.2.0 can"))
            continue
        if new_error is not None:
            mismatches.append((url, sha, f"0.2.0 cannot parse this page: {new_error}"))
            continue
        old = [
            tuple(row)
            for row in src.execute(
                f"SELECT {fields} FROM talk_cards WHERE blob_sha256 = ? AND url = ? "
                f"ORDER BY position",
                (sha, url),
            )
        ]
        new = [tuple(row[f] for f in _CARD_FIELDS) for row in store.cards(page)]
        if old != new:
            mismatches.append((url, sha, _describe_difference(old, new)))
    return mismatches


def _describe_difference(old: list[tuple], new: list[tuple]) -> str:
    old_ids = [card[0] for card in old]
    new_ids = [card[0] for card in new]
    if old_ids != new_ids:
        gone, added = set(old_ids) - set(new_ids), set(new_ids) - set(old_ids)
        if not gone and not added:
            return f"same {len(new)} talks in a different order"
        return f"{len(old)} talks in 0.1.0, {len(new)} now ({len(gone)} missing, {len(added)} new)"
    edited = sum(a != b for a, b in zip(old, new))
    return f"{edited} of {len(new)} talks have different fields"


@contextmanager
def _read_only(path: Path) -> Iterator[sqlite3.Connection]:
    # 0.1.0 archives are in WAL mode, and even a read-only connection to one
    # creates -wal and -shm files next to it. immutable=1 reads the file alone
    # and creates nothing, which is safe because source_schema() refuses a
    # source with a log, i.e. one that has data outside the file or a writer.
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _link(src: Path, dst: Path, what: str) -> None:
    """Make ``dst`` another name for ``src``'s file, never replacing an existing ``dst``."""
    try:
        os.link(src, dst)
        return
    except FileExistsError:
        raise OutputExistsError(dst, what) from None
    except OSError as exc:
        # No hard links on this file system: copy instead.
        logger.info("cannot hard-link %s to %s (%s); copying", src, dst, exc)
    try:
        with open(dst, "xb") as fout:
            try:
                with open(src, "rb") as fin:
                    shutil.copyfileobj(fin, fout)
            except BaseException:
                dst.unlink(missing_ok=True)
                raise
    except FileExistsError:
        raise OutputExistsError(dst, what) from None
    shutil.copystat(src, dst)


def _remove(path: Path) -> None:
    for name in (str(path), f"{path}-wal", f"{path}-shm"):
        Path(name).unlink(missing_ok=True)
