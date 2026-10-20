"""Command-line interface for :mod:`apigrunn_archiver_db_migrator`.

Exit codes: 0 migrated (with ``--dry-run``: would migrate), 1 the migrated
archive failed verification and was thrown away, 2 usage error or refused.
In every case but 0 without ``--dry-run``, nothing was written.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from apigrunn_archiver import __version__ as archiver_version
from apigrunn_archiver import default_db_path

from ._version import __version__
from .errors import MigratorError, VerificationError
from .migration import TARGET_SCHEMA, MigrationResult, dry_run, migrate, migrate_in_place

EXIT_VERIFY_FAILED = 1
EXIT_REFUSED = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apigrunn-archiver-db-migrator",
        description=(
            "Migrate an apigrunn-archiver 0.1.0 archive (schema 1 or 2) to the format "
            "apigrunn-archiver 0.2.0 reads (schema 3). The source is never modified."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__} (apigrunn-archiver {archiver_version})",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log each page parsed")
    parser.add_argument(
        "source",
        nargs="?",
        help="archive to migrate (default: $APIGRUNN_ARCHIVE or XDG data dir)",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("-o", "--output", help="write the migrated archive to this new file")
    mode.add_argument(
        "--in-place",
        action="store_true",
        help="replace SOURCE, keeping the original next to it as SOURCE.schemaN.bak",
    )
    mode.add_argument(
        "--dry-run", action="store_true", help="migrate and verify, but keep nothing"
    )
    parser.add_argument(
        "--drop-error-bodies",
        action="store_true",
        help="discard HTML returned only by error responses, which 0.2.0 does not keep "
        "(default: keep it)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.ERROR,
        format="%(levelname)s: %(message)s",
    )
    source = args.source or default_db_path()

    try:
        if args.dry_run:
            result = dry_run(source, drop_error_bodies=args.drop_error_bodies)
        elif args.in_place:
            result = migrate_in_place(source, drop_error_bodies=args.drop_error_bodies)
        else:
            result = migrate(source, args.output, drop_error_bodies=args.drop_error_bodies)
    except VerificationError as exc:
        print(exc, file=sys.stderr)
        return EXIT_VERIFY_FAILED
    except MigratorError as exc:
        print(exc, file=sys.stderr)
        return EXIT_REFUSED
    except OSError as exc:
        print(f"error: {exc}; nothing was written", file=sys.stderr)
        return EXIT_REFUSED

    _report(result)
    return 0


def _report(result: MigrationResult) -> None:
    written_by = ", ".join(result.tool_versions) or "no runs"
    verb = "migrated" if result.output else "would migrate"
    print(f"{verb} {result.source} (schema {result.source_schema}, apigrunn-archiver {written_by})")
    if result.output:
        print(f"      to {result.output} (schema {TARGET_SCHEMA}, apigrunn-archiver {archiver_version})")
    else:
        print(f"      to schema {TARGET_SCHEMA} (apigrunn-archiver {archiver_version}); "
              f"dry run, nothing written")
    print(f"  {result.runs} runs, {result.snapshots} snapshots, {result.blobs} blobs")
    print(f"  talk index rebuilt: {result.page_versions} page versions, "
          f"{result.talk_versions} talk versions, {len(result.parse_errors)} unparseable pages")
    print("  verified: every blob re-hashed, runs and snapshots match the source")
    if result.backup:
        print(f"  original kept as {result.backup}")
    if result.output and not result.backup:
        print(f"  use it with: apigrunn-archiver --db {result.output} runs")

    if result.error_bodies:
        if result.error_bodies_dropped:
            print(f"note: discarded {result.error_bodies} page bodies that only error "
                  f"responses returned", file=sys.stderr)
        else:
            print(f"note: kept {result.error_bodies} page bodies that only error responses "
                  f"returned; 0.2.0 does not show them (--drop-error-bodies discards them)",
                  file=sys.stderr)
    for url, sha, message in result.parse_errors:
        print(f"warning: {url} ({sha[:12]}) could not be parsed and is kept as an "
              f"unparseable page: {message}", file=sys.stderr)
    for url, sha, reason in result.index_mismatches:
        print(f"warning: {url} ({sha[:12]}) differs from the 0.1.0 talk index: {reason}",
              file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
