# apigrunn-archiver-db-migrator

Upgrades an [apigrunn-archiver](https://github.com/evoso-software/apigrunn-archiver)
0.1.0 archive to the format 0.2.0 reads.

0.2.0 changed the database schema and has no migrations, so it refuses older archives:

```
[ARCHIVER-E300] ~/.local/share/apigrunn-archiver/archive.db is at schema version 2, but this
version of apigrunn-archiver only understands version 3. ...
```

0.1.0 kept the HTML of every response it received, and everything 0.2.0 stores is
either copied from that archive or derived from that HTML. So the migration loses
nothing.

## Usage

The migrator is one file, `apigrunn_archiver_db_migrator.py`, with no dependencies:
any Python 3.11 or newer runs it. Download it and run it; nothing to install.

```bash
python3 apigrunn_archiver_db_migrator.py --dry-run          # migrate and verify, keep nothing
python3 apigrunn_archiver_db_migrator.py -o archive-v3.db   # write a new file next to it
python3 apigrunn_archiver_db_migrator.py --in-place         # replace it, keep the original
python3 apigrunn_archiver_db_migrator.py old.db -o new.db   # any other archive
```

Installed as a package (`pip install .`, `uv tool install .`) the same thing is the
`apigrunn-archiver-db-migrator` command.

The source defaults to the archiver's own (`$APIGRUNN_ARCHIVE`, else
`$XDG_DATA_HOME/apigrunn-archiver/archive.db`). Exactly one of `-o`, `--in-place` or
`--dry-run` is required.

```
$ python3 apigrunn_archiver_db_migrator.py --in-place
migrated ~/.local/share/apigrunn-archiver/archive.db (schema 2, apigrunn-archiver 0.1.0)
      to ~/.local/share/apigrunn-archiver/archive.db (schema 3, apigrunn-archiver 0.2.0)
  2 runs, 16 snapshots, 8 blobs
  talk index rebuilt: 8 page versions, 66 talk versions, 0 unparseable pages
  verified: every blob re-hashed, runs and snapshots match the source
  original kept as ~/.local/share/apigrunn-archiver/archive.db.schema2.bak
```

Exit codes: 0 migrated, 1 the result failed verification (nothing written), 2 refused
or usage error (nothing written), 3 crashed (a bug, see [Bug reports](#bug-reports)).

```python
from apigrunn_archiver_db_migrator import migrate, migrate_in_place, dry_run

result = migrate("archive.db", "archive-v3.db")
result.parse_errors, result.index_mismatches   # anything worth a look
```

## Bug reports

If the migrator crashes (exit code 3), it can send an anonymous bug report. It is opt-in:

- `--report-bugs` or `APIGRUNN_MIGRATOR_REPORT_BUGS=1`: send without asking.
- `--no-report-bugs` or `APIGRUNN_MIGRATOR_REPORT_BUGS=0`: never send, never ask.
- Otherwise it shows the exact report and asks, but only when run in a terminal.
  In scripts and CI nothing is sent.

A report holds the migrator's version and the archiver version it targets, the Python version,
OS and CPU architecture, the mode (`output`, `in-place` or `dry-run`), the exception's
type, and the module, function and line number of each frame of its traceback. It
never holds the exception's message, file paths, URLs or anything read from an
archive. Refusals and failed verifications are not bugs and are never reported.

## Safety

- The source is only ever opened read-only. It is refused if a `-wal` or `-journal`
  file next to it shows an archiver may still be writing to it.
- The new archive is built in a temporary file next to the output and moved into
  place only after it passes verification. Nothing is ever overwritten: an existing
  output or backup is an error.
- `--in-place` keeps the original file as `archive.db.schema2.bak` (a hard link where
  the file system allows it). The new archive replaces it in one rename, so the path
  always holds a complete archive.

## What is migrated, and how

| schema 2 (0.1.0)                  | schema 3 (0.2.0)                                        |
|-----------------------------------|---------------------------------------------------------|
| `runs`                            | copied unchanged, `tool_version` stays `0.1.0`          |
| `blobs`                           | copied unchanged                                        |
| `snapshots.changed`               | dropped: 0.2.0 computes it from the talks, not bytes    |
| `snapshots.blob_sha256`           | also becomes `body_sha256`, so every snapshot is exact  |
| `parsed_pages`, `talk_cards`      | not converted: the index is rebuilt by 0.2.0's parser   |

The talk index is rebuilt from the stored HTML, not converted. If the source has a
0.1.0 index (schema 2), it is compared with the rebuilt one, and any page where they
disagree is reported as a warning.

To do that without installing apigrunn-archiver, the script carries a copy of the parts
of 0.2.0 it needs: the schema, the hashes the index is keyed by, and the talk parser.
0.2.0 parses with BeautifulSoup; the copy rebuilds the same tree on the standard
library's `HTMLParser`, the tokenizer BeautifulSoup's `html.parser` builder uses too.
The tests hold that copy to the real 0.2.0 (see [Development](#development)), so the
archive comes out as if 0.2.0 had built its index itself.

Things that behave differently after migrating:

- **Error responses.** 0.1.0 kept their body; 0.2.0 keeps only status, error and
  body hash. Migrated error snapshots no longer point at a body. Bodies returned only
  by errors stay in the archive, unshown, unless you pass `--drop-error-bodies`.
- **Noise.** If 0.1.0 stored several copies of a page that differ only in nonces or
  build hashes, all of them are kept. Each snapshot keeps its own exact HTML, and 0.2.0
  no longer counts those copies as changes.
- **`changed` counts** in `runs` and `history` now mean "the talks changed", so they
  can be lower than 0.1.0 reported.
- **Schema 1** archives (written by 0.1.0 before it had a talk index) are migrated
  too. They get a talk index; there is just nothing to compare it with.

## Development

```bash
nix develop -c uv run pytest
```

The tests use a copy of a real 0.1.0 archive (`tests/fixtures/archive_v0.1.0.db`).
They also build 0.1.0 archives with `tests/legacy.py`, which copies 0.1.0's schema and
write path, to cover errors, 304s, noise, unparseable pages and refusals.

apigrunn-archiver is a development dependency only, pinned to the 0.2.0 commit the
script's copy was taken from. `tests/test_equivalence.py` checks the copy against it:

- the pin itself, the schema both create, and `default_db_path`;
- whole migrations: 0.2.0's own `ensure_index` rebuilds each migrated archive's index,
  which must match the script's in every table, row for row;
- the parser, card for card: on the real pages, on hand-picked edge cases (entities,
  character references, unclosed and stray tags, comments, CDATA, `<script>`,
  `<template>`, nesting), and on thousands of generated and mutated pages.

Do not bump the pin without a reason: a 0.2.x with another schema or parser needs the
copy updated and a new migration step, and these tests will say so.
