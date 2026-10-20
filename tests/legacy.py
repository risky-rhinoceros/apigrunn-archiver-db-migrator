"""Write archives the way apigrunn-archiver 0.1.0 did, for the tests.

The schema below is 0.1.0's ``MIGRATIONS`` verbatim (commit 9ddfe7c);
``schema`` is 1 for an archive from before the talk index, 2 after it.
:class:`LegacyArchive` reproduces 0.1.0's ``Store.add_snapshot`` and
``save_parse``, so tests can build any history: errors, 304s, pages that
changed only in noise.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self

from apigrunn_archiver.index import parse_page
from apigrunn_archiver.targets import track_url

MIGRATIONS = (
    """
    CREATE TABLE runs (
        id           INTEGER PRIMARY KEY,
        started_at   TEXT NOT NULL,
        finished_at  TEXT,
        tool_version TEXT NOT NULL,
        status       TEXT NOT NULL
    );

    CREATE TABLE blobs (
        sha256      TEXT PRIMARY KEY,
        size        INTEGER NOT NULL,
        compression TEXT NOT NULL,
        data        BLOB NOT NULL
    );

    CREATE TABLE snapshots (
        run_id        INTEGER NOT NULL REFERENCES runs(id),
        url           TEXT NOT NULL,
        final_url     TEXT,
        fetched_at    TEXT NOT NULL,
        status_code   INTEGER,
        error         TEXT,
        headers       TEXT,
        etag          TEXT,
        last_modified TEXT,
        blob_sha256   TEXT REFERENCES blobs(sha256),
        changed       INTEGER NOT NULL,
        PRIMARY KEY (run_id, url)
    );

    CREATE INDEX idx_snapshots_url ON snapshots(url, fetched_at);
    """,
    """
    CREATE TABLE parsed_pages (
        blob_sha256    TEXT NOT NULL REFERENCES blobs(sha256),
        url            TEXT NOT NULL,
        parser_version INTEGER NOT NULL,
        cards          INTEGER NOT NULL,
        error          TEXT,
        PRIMARY KEY (blob_sha256, url)
    );

    CREATE TABLE talk_cards (
        blob_sha256   TEXT NOT NULL,
        url           TEXT NOT NULL,
        position      INTEGER NOT NULL,
        video_id      TEXT NOT NULL,
        title         TEXT NOT NULL,
        speaker       TEXT NOT NULL,
        description   TEXT NOT NULL,
        talk_url      TEXT NOT NULL,
        year          INTEGER,
        thumbnail_url TEXT,
        PRIMARY KEY (blob_sha256, url, video_id),
        FOREIGN KEY (blob_sha256, url)
            REFERENCES parsed_pages(blob_sha256, url) ON DELETE CASCADE
    );

    CREATE INDEX idx_talk_cards_video ON talk_cards(video_id);
    """,
)

CONTENT_TYPE = "text/html; charset=utf-8"
_EPOCH = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)


class LegacyArchive:
    """A 0.1.0 archive at schema ``1`` or ``2``, filled one snapshot at a time."""

    def __init__(self, path: Path, schema: int = 2) -> None:
        self.path = path
        self.schema = schema
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode = WAL")
        for target, script in enumerate(MIGRATIONS[:schema], start=1):
            self.conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {target};\nCOMMIT;")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def run(self, *, status: str = "ok") -> int:
        run_id = (self.conn.execute("SELECT MAX(id) FROM runs").fetchone()[0] or 0) + 1
        at = self._at(run_id)
        with self.conn:
            self.conn.execute(
                "INSERT INTO runs (id, started_at, finished_at, tool_version, status) "
                "VALUES (?, ?, ?, '0.1.0', ?)",
                (run_id, at, None if status == "running" else at, status),
            )
        return run_id

    def snapshot(
        self,
        run_id: int,
        track: str,
        body: bytes | None,
        *,
        status: int | None = 200,
        error: str | None = None,
        not_modified: bool = False,
    ) -> str | None:
        """Record one fetch as 0.1.0 did. Returns the blob it points at."""
        url = track_url(track)
        previous = self.conn.execute(
            "SELECT blob_sha256, etag FROM snapshots WHERE url = ? AND error IS NULL "
            "AND blob_sha256 IS NOT NULL ORDER BY fetched_at DESC, run_id DESC LIMIT 1",
            (url,),
        ).fetchone()
        with self.conn:
            if not_modified:
                sha = previous[0] if previous else None
                status = 304
            elif body is not None:
                sha = hashlib.sha256(body).hexdigest()
                self.conn.execute(
                    "INSERT OR IGNORE INTO blobs (sha256, size, compression, data) "
                    "VALUES (?, ?, 'gzip', ?)",
                    (sha, len(body), gzip.compress(body, compresslevel=9, mtime=0)),
                )
            else:
                sha = None
            changed = error is None and sha is not None and (previous is None or previous[0] != sha)
            headers = [["content-type", CONTENT_TYPE], ["etag", f'"{track}-{run_id}"']]
            self.conn.execute(
                """
                INSERT INTO snapshots (run_id, url, final_url, fetched_at, status_code, error,
                                       headers, etag, last_modified, blob_sha256, changed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, url, url, self._at(run_id), status, error,
                 None if status is None else json.dumps(headers),
                 f'"{track}-{run_id}"', None, sha, int(changed)),
            )
        if self.schema >= 2 and error is None and sha is not None and body is not None:
            self._index(sha, url, body)
        return sha

    def _index(self, sha: str, url: str, body: bytes) -> None:
        cards, error = parse_page(body, CONTENT_TYPE, url)
        with self.conn:
            self.conn.execute("DELETE FROM parsed_pages WHERE blob_sha256 = ? AND url = ?",
                              (sha, url))
            self.conn.execute(
                "INSERT INTO parsed_pages (blob_sha256, url, parser_version, cards, error) "
                "VALUES (?, ?, 1, ?, ?)",
                (sha, url, len(cards), error),
            )
            self.conn.executemany(
                """
                INSERT INTO talk_cards (blob_sha256, url, position, video_id, title, speaker,
                                        description, talk_url, year, thumbnail_url)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [(sha, url, i, c.video_id, c.title, c.speaker, c.description, c.url, c.year,
                  c.thumbnail_url) for i, c in enumerate(cards)],
            )

    @staticmethod
    def _at(run_id: int) -> str:
        return (_EPOCH + timedelta(days=run_id)).isoformat(timespec="seconds")
