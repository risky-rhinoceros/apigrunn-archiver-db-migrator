#!/usr/bin/env python3
"""Migrate an apigrunn-archiver 0.1.0 archive to the format 0.2.0 reads.

A single file with no dependencies beyond Python 3.11's standard library::

    python3 apigrunn_archiver_db_migrator.py --dry-run     # migrate and verify, keep nothing
    python3 apigrunn_archiver_db_migrator.py --in-place    # replace it, keep the original
    python3 apigrunn_archiver_db_migrator.py old.db -o new.db

It can also be imported::

    >>> from apigrunn_archiver_db_migrator import migrate
    >>> result = migrate("archive.db", "archive-v3.db")

apigrunn-archiver 0.2.0 has no migrations and refuses older archives. This
script rebuilds one from the HTML 0.1.0 stored, verifies it, and only then
moves it into place.

0.1.0 wrote schema 1 (runs, blobs, snapshots) or, from its talk index
onwards, schema 2 (plus parsed_pages and talk_cards). 0.2.0 reads only
schema 3. Everything schema 3 holds is either copied as is or derived from
the stored HTML, which 0.1.0 kept for every response it received:

- ``runs`` and ``blobs`` are copied unchanged, ``tool_version`` included.
- ``snapshots`` lose ``changed``, which 0.2.0 computes (by talks, not
  bytes), and gain ``body_sha256``. 0.1.0 stored every body it received, so
  that is the snapshot's own blob and every migrated snapshot is exact.
- Error responses keep their status, error and body hash but no longer
  point at a body, as in 0.2.0. Bodies only they returned are kept unless
  asked to drop them; nothing in 0.2.0 shows them.
- The talk index (``talk_versions``, ``page_versions``, ``page_talks``,
  ``blob_parses``) is not converted but rebuilt from the HTML. A schema 2
  index is only compared against the result, as a cross-check.

The parts of apigrunn-archiver 0.2.0 that the migration needs (its schema,
its talk parser and the hashes its index is keyed by) are copied into the
"apigrunn-archiver 0.2.0" section below, so nothing has to be installed.
The test suite checks them against the real 0.2.0, which it installs as a
development dependency: the same schema, the same parse of every page, and
the same index, row for row.

The source is only ever opened read-only, and the migrated archive is built
in a temporary file next to the output and verified before it is moved into
place, so a failure at any point leaves no partial output.

Exit codes: 0 migrated (with ``--dry-run``: would migrate), 1 the migrated
archive failed verification and was thrown away, 2 usage error or refused.
In every case but 0 without ``--dry-run``, nothing was written. 3 means the
tool crashed, which is a bug (see "Bug reports" below).
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html.entities
import json
import logging
import os
import platform
import re
import shutil
import sqlite3
import sys
import tempfile
import traceback
import urllib.request
import uuid
import zlib
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse, urlsplit

__version__ = "0.1.0"

logger = logging.getLogger(__name__)


# =================================================================== errors
#
# Like apigrunn-archiver's own errors, each carries a stable ``code`` prefixed
# onto its message. Give a new condition a new code rather than reusing one.
#
# =====================  ===============================================
# ``MIGRATOR-E000``      :class:`MigratorError` (base, not raised)
# ``MIGRATOR-E100``      :class:`SourceError`
# ``MIGRATOR-E200``      :class:`OutputExistsError`
# ``MIGRATOR-E300``      :class:`VerificationError`
# ``MIGRATOR-E400``      retired: was "installed apigrunn-archiver unsupported"
# =====================  ===============================================
#
# Whichever is raised, the source archive has not been modified.


class MigratorError(Exception):
    """Base class for every error raised by this module."""

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


class PathError(Exception):
    """Error to indicate that a path is invalid, this can be recoverable in some cases."""

    def __init__(self, message: str, path: Path, recoverable: bool):
        super().__init__(message)
        self.path = path
        self.recoverable = True


# ====================================================== apigrunn-archiver 0.2.0
#
# Copied from apigrunn-archiver 0.2.0 (commit 5888f95), its ``store``,
# ``parser`` and ``index`` modules, so that this file stands alone. Only
# what the migration needs is here. The one real change is the parser:
# 0.2.0 parses with BeautifulSoup's "html.parser" builder, which this
# reproduces on the standard library's HTMLParser, the tokenizer that
# builder uses too. tests/test_equivalence.py holds every piece of this
# section to the real 0.2.0; change nothing here without it.

#: The apigrunn-archiver version this script writes archives for.
ARCHIVER_VERSION = "0.2.0"
#: The schema written by apigrunn-archiver 0.2.0, the only one this tool produces.
TARGET_SCHEMA = 3
#: The version of 0.2.0's talk parser, stored with every parsed page.
PARSER_VERSION = 1

_SCHEMA = """
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

-- One row per distinct set of talk fields; sha256 is over those fields.
CREATE TABLE talk_versions (
    sha256        TEXT PRIMARY KEY,
    video_id      TEXT NOT NULL,
    title         TEXT NOT NULL,
    speaker       TEXT NOT NULL,
    description   TEXT NOT NULL,
    url           TEXT NOT NULL,
    year          INTEGER,
    thumbnail_url TEXT
);

-- One row per distinct ordered list of talk versions; sha256 is over the list.
CREATE TABLE page_versions (
    sha256 TEXT PRIMARY KEY,
    cards  INTEGER NOT NULL
);

CREATE TABLE page_talks (
    page_sha256 TEXT NOT NULL REFERENCES page_versions(sha256),
    position    INTEGER NOT NULL,
    talk_sha256 TEXT NOT NULL REFERENCES talk_versions(sha256),
    PRIMARY KEY (page_sha256, position)
);

CREATE INDEX idx_page_talks_talk ON page_talks(talk_sha256);

-- What the parser made of a stored blob: a page version, or an error.
CREATE TABLE blob_parses (
    blob_sha256    TEXT NOT NULL REFERENCES blobs(sha256),
    url            TEXT NOT NULL,
    parser_version INTEGER NOT NULL,
    page_sha256    TEXT REFERENCES page_versions(sha256),
    error          TEXT,
    PRIMARY KEY (blob_sha256, url)
);

CREATE INDEX idx_blob_parses_page ON blob_parses(url, page_sha256);

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
    body_sha256   TEXT,
    blob_sha256   TEXT REFERENCES blobs(sha256),
    PRIMARY KEY (run_id, url)
);

CREATE INDEX idx_snapshots_url ON snapshots(url, fetched_at);

-- Snapshots with their page version and whether it differs from the previous
-- good snapshot of the same URL. Computed rather than stored, so a reindex
-- with a new parser corrects it everywhere. A page that could not be parsed
-- is compared by its blob instead.
CREATE VIEW snapshot_states AS
WITH good AS (
    SELECT s.run_id, s.url, p.page_sha256,
           COALESCE(p.page_sha256, 'blob:' || s.blob_sha256) AS content_key,
           LAG(COALESCE(p.page_sha256, 'blob:' || s.blob_sha256))
               OVER (PARTITION BY s.url ORDER BY s.fetched_at, s.run_id) AS previous_key
    FROM snapshots s
    LEFT JOIN blob_parses p ON p.blob_sha256 = s.blob_sha256 AND p.url = s.url
    WHERE s.error IS NULL AND s.blob_sha256 IS NOT NULL
)
SELECT s.*, g.page_sha256,
       CASE WHEN g.run_id IS NULL THEN 0
            ELSE g.previous_key IS NULL OR g.previous_key != g.content_key END AS changed
FROM snapshots s
LEFT JOIN good g ON g.run_id = s.run_id AND g.url = s.url;
"""

_TALK_FIELDS = ("video_id", "title", "speaker", "description", "url", "year", "thumbnail_url")
_INVALID_SEPARATORS = ("\\", "")


def default_db_path() -> Path:
    """The archiver's default archive, honouring ``$APIGRUNN_ARCHIVE`` and XDG."""
    override = os.environ.get("APIGRUNN_ARCHIVE")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "share"
    return root / "apigrunn-archiver" / "archive.db"


class _IntegrityError(Exception):
    """0.2.0's ``IntegrityError``: a blob is missing or does not match its hash."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def _create_archive(path: Path) -> None:
    """Create 0.2.0's schema in the empty database ``path``, as its ``Store`` does."""
    with closing(_connect(path)) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(f"BEGIN;\n{_SCHEMA}\nPRAGMA user_version = {TARGET_SCHEMA};\nCOMMIT;")


def _connect(path: Path) -> sqlite3.Connection:
    """A connection set up the way 0.2.0's ``Store`` sets up its own."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _blob(conn: sqlite3.Connection, sha256: str) -> bytes:
    """A blob's original bytes, checked against their hash."""
    row = conn.execute(
        "SELECT size, compression, data FROM blobs WHERE sha256 = ?", (sha256,)
    ).fetchone()
    if row is None:
        raise _IntegrityError(f"blob {sha256} is referenced but missing from the archive")
    try:
        if row["compression"] == "gzip":
            content = gzip.decompress(row["data"])
        elif row["compression"] == "none":
            content = row["data"]
        else:
            raise ValueError(f"unknown blob compression {row['compression']!r}")
    except (OSError, EOFError, zlib.error, ValueError) as exc:
        raise _IntegrityError(f"blob {sha256} cannot be decompressed: {exc}") from exc
    if hashlib.sha256(content).hexdigest() != sha256 or len(content) != row["size"]:
        raise _IntegrityError(f"blob {sha256} does not match its hash; the archive is damaged")
    return content


def _talk_sha(talk: ParsedCard | sqlite3.Row) -> str:
    """Hash of the fields that make up a talk version; the track is not one."""
    get = talk.__getitem__ if isinstance(talk, sqlite3.Row) else talk.__getattribute__
    canonical = json.dumps(
        [get(name) for name in _TALK_FIELDS], ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _page_sha(talk_shas: Sequence[str]) -> str:
    return hashlib.sha256("\n".join(talk_shas).encode("ascii")).hexdigest()


def _unparsed_blobs(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """``(sha256, url, content_type)`` for stored page bodies not yet parsed."""
    rows = conn.execute(
        """
        SELECT s.blob_sha256 AS sha, s.url AS url, MIN(s.run_id) AS run_id
        FROM snapshots s
        LEFT JOIN blob_parses p
               ON p.blob_sha256 = s.blob_sha256 AND p.url = s.url
              AND p.parser_version = ?
        WHERE s.error IS NULL AND s.blob_sha256 IS NOT NULL AND p.url IS NULL
        GROUP BY s.blob_sha256, s.url
        ORDER BY run_id, s.url
        """,
        (PARSER_VERSION,),
    ).fetchall()
    out = []
    for row in rows:
        headers = conn.execute(
            "SELECT headers FROM snapshots WHERE run_id = ? AND url = ?",
            (row["run_id"], row["url"]),
        ).fetchone()["headers"]
        content_type = next(
            (v for k, v in json.loads(headers or "[]") if k.lower() == "content-type"), ""
        )
        out.append((row["sha"], row["url"], content_type))
    return out


def _put_page_version(conn: sqlite3.Connection, cards: Sequence[ParsedCard]) -> str:
    """Store a page version and its talk versions unless already present."""
    talk_shas = []
    for card in cards:
        sha = _talk_sha(card)
        talk_shas.append(sha)
        conn.execute(
            """
            INSERT OR IGNORE INTO talk_versions (sha256, video_id, title, speaker,
                                                 description, url, year, thumbnail_url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sha,
                card.video_id,
                card.title,
                card.speaker,
                card.description,
                card.url,
                card.year,
                card.thumbnail_url,
            ),
        )
    page = _page_sha(talk_shas)
    cursor = conn.execute(
        "INSERT OR IGNORE INTO page_versions (sha256, cards) VALUES (?, ?)",
        (page, len(talk_shas)),
    )
    if cursor.rowcount == 1:
        conn.executemany(
            "INSERT INTO page_talks (page_sha256, position, talk_sha256) VALUES (?, ?, ?)",
            [(page, i, sha) for i, sha in enumerate(talk_shas)],
        )
    return page


def _ensure_index(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """Parse every stored page body not yet parsed, as 0.2.0's ``ensure_index``.

    Returns ``(url, blob_sha256, message)`` for each page that failed to parse.
    """
    errors: list[tuple[str, str, str]] = []
    for sha, url, content_type in _unparsed_blobs(conn):
        try:
            cards, error = _parse_page(_blob(conn, sha), content_type, url)
        except _IntegrityError as exc:
            logger.warning("%s (%s): %s", url, sha[:12], exc)
            cards, error = [], exc.message
        if error is not None:
            errors.append((url, sha, error))
        with conn:
            page = _put_page_version(conn, cards) if error is None else None
            conn.execute(
                """
                INSERT OR REPLACE INTO blob_parses
                    (blob_sha256, url, parser_version, page_sha256, error)
                VALUES (?, ?, ?, ?, ?)
                """,
                (sha, url, PARSER_VERSION, page, error),
            )
    return errors


def _cards(conn: sqlite3.Connection, page_sha256: str) -> list[sqlite3.Row]:
    """The talk versions on a page version, in page order."""
    return conn.execute(
        """
        SELECT t.video_id, t.title, t.speaker, t.description, t.url AS talk_url,
               t.year, t.thumbnail_url
        FROM page_talks pt JOIN talk_versions t ON t.sha256 = pt.talk_sha256
        WHERE pt.page_sha256 = ?
        ORDER BY pt.position
        """,
        (page_sha256,),
    ).fetchall()


def _bad_index_rows(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """``(sha256, reason)`` for talk and page versions that do not match their
    hash, or that reference rows which are missing."""
    bad: list[tuple[str, str]] = []
    for row in conn.execute("SELECT * FROM talk_versions ORDER BY sha256"):
        if _talk_sha(row) != row["sha256"]:
            bad.append((row["sha256"], "talk version does not match its hash"))
    pages = {
        row["sha256"]: (row["cards"], [])
        for row in conn.execute("SELECT sha256, cards FROM page_versions")
    }
    for row in conn.execute(
        """
        SELECT pt.page_sha256, pt.talk_sha256, t.sha256 AS found
        FROM page_talks pt LEFT JOIN talk_versions t ON t.sha256 = pt.talk_sha256
        ORDER BY pt.page_sha256, pt.position
        """
    ):
        if row["found"] is None:
            bad.append(
                (row["page_sha256"], f"page version lists missing talk {row['talk_sha256']}")
            )
        if row["page_sha256"] in pages:
            pages[row["page_sha256"]][1].append(row["talk_sha256"])
    for sha, (cards, talks) in sorted(pages.items()):
        if len(talks) != cards:
            bad.append((sha, f"page version has {len(talks)} of its {cards} talks"))
        elif _page_sha(talks) != sha:
            bad.append((sha, "page version does not match its hash"))
    for row in conn.execute(
        """
        SELECT p.blob_sha256, p.page_sha256 FROM blob_parses p
        LEFT JOIN page_versions v ON v.sha256 = p.page_sha256
        WHERE p.page_sha256 IS NOT NULL AND v.sha256 IS NULL
        """
    ):
        bad.append(
            (row["blob_sha256"], f"parse points at missing page version {row['page_sha256']}")
        )
    return bad


# ----------------------------------------------------------- the talk parser
#
# The markup it targets ("Blast from the past" on every track page) looks like::
#
#     <div class="talks-year-group">
#       <span class="talks-year-badge y2025">2025</span>
#       <div class="talks-lane" id="talksLane2025">
#         <a href="https://www.youtube.com/watch?v=tP_kg20VAho" class="talk-card"
#            data-title="..." data-speaker="..." data-desc="...">
#           <div class="talk-thumb"><img src="https://img.youtube.com/vi/..."/></div>

_CHARSET = re.compile(r"charset=([\w.:-]+)", re.IGNORECASE)
_YEAR_CLASS = re.compile(r"\by(\d{4})\b")
_YEAR_TEXT = re.compile(r"(\d{4})")


@dataclass(frozen=True, slots=True)
class ParsedCard:
    """One talk as it appears on one track page."""

    video_id: str
    title: str
    speaker: str
    description: str
    url: str
    year: int | None
    thumbnail_url: str | None
    track: str


class _PageParseError(Exception):
    """0.2.0's ``ParseError``: the page has talk cards but none could be extracted."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def _decode_html(raw: bytes, content_type: str) -> str:
    """Decode a body using the charset the server declared, else UTF-8."""
    match = _CHARSET.search(content_type or "")
    encoding = match.group(1) if match else "utf-8"
    try:
        return raw.decode(encoding, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _track_slug(url: str) -> str:
    """``https://www.aigrunn.org/tech`` -> ``tech``."""
    return urlsplit(url).path.strip("/") or "index"


def _parse_page(raw: bytes, content_type: str, url: str) -> tuple[list[ParsedCard], str | None]:
    """Parse one page body: ``(cards, None)``, or ``([], message)`` on failure."""
    try:
        return _parse_track_page(_decode_html(raw, content_type), _track_slug(url)), None
    except _PageParseError as exc:
        logger.warning("%s: %s", url, exc)
        return [], exc.message


def _parse_track_page(html: str, track_slug: str) -> list[ParsedCard]:
    """Parse every talk card on a track page.

    Malformed cards are skipped. Raises :class:`_PageParseError` only when the
    page plainly contains talk cards but none could be extracted.
    """
    root = _HTMLTree.parse(html)

    cards: list[ParsedCard] = []
    seen: set[str] = set()

    for group in root.select("div", "talks-year-group"):
        year = _group_year(group)
        for anchor in group.select("a", "talk-card"):
            card = _parse_card(anchor, year=year, track_slug=track_slug)
            if card is None:
                continue
            if card.video_id in seen:
                logger.debug("duplicate talk %s on /%s, keeping first", card.video_id, track_slug)
                continue
            seen.add(card.video_id)
            cards.append(card)

    total_anchors = sum(1 for _ in root.select("a", "talk-card"))
    if not cards:
        if total_anchors or "talk-card" in html:
            raise _PageParseError(
                f"/{track_slug}: found {total_anchors} talk cards but could not extract any; "
                "the aigrunn.org markup has probably changed"
            )
        logger.info("/%s: no talk archive on this page", track_slug)
    elif total_anchors > len(cards):
        logger.warning(
            "/%s: %d talk cards on the page but only %d inside a year group",
            track_slug,
            total_anchors,
            len(cards),
        )

    return cards


def _group_year(group: _Element) -> int | None:
    badge = next(group.select("span", "talks-year-badge"), None)
    if badge is None:
        return None

    for cls in badge.classes:
        match = _YEAR_CLASS.fullmatch(cls)
        if match:
            return int(match.group(1))

    match = _YEAR_TEXT.search("".join(badge.strings(strip=True)))
    return int(match.group(1)) if match else None


def _parse_card(anchor: _Element, *, year: int | None, track_slug: str) -> ParsedCard | None:
    href = anchor.attrs.get("href", "").strip()
    video_id = _youtube_video_id(href)
    title = _attr_or_text(anchor, "data-title", "talk-title")

    if not video_id or not title:
        logger.warning(
            "/%s: skipping talk card without a video id or title (href=%r)", track_slug, href
        )
        return None

    # Like the CSS selector "div.talk-thumb img": the div may enclose the anchor.
    thumb = next(
        (img for img in anchor.select("img") if img.has_ancestor("div", "talk-thumb")), None
    )
    thumbnail_url = thumb.attrs.get("src", "").strip() or None if thumb is not None else None

    return ParsedCard(
        video_id=video_id,
        title=title,
        speaker=_attr_or_text(anchor, "data-speaker", "talk-speaker"),
        description=_attr_or_text(anchor, "data-desc", "talk-desc"),
        url=href,
        year=year,
        thumbnail_url=thumbnail_url,
        track=track_slug,
    )


def _youtube_video_id(url: str) -> str | None:
    """Extract the video id from a YouTube watch or short-form URL."""
    if not url:
        return None
    parts = urlparse(url)
    host = parts.netloc.lower().removeprefix("www.")
    if host == "youtu.be":
        return parts.path.lstrip("/").split("/")[0] or None
    if host in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
        if parts.path.startswith(("/embed/", "/v/", "/shorts/")):
            return parts.path.split("/")[2] or None
        values = parse_qs(parts.query).get("v")
        return values[0] if values else None
    return None


def _attr_or_text(tag: _Element, attribute: str, div_class: str) -> str:
    """Read a ``data-*`` attribute, falling back to the rendered ``div``.

    The fallback applies only when the attribute is *absent*: one card carries
    ``data-speaker=""`` next to a ``<div class="talk-speaker">aiGrunn 2024</div>``
    label, which is a year, not a speaker.
    """
    value = tag.attrs.get(attribute)
    if value is not None:
        return _clean(value)
    found = next(tag.select("div", div_class), None)
    return _clean(" ".join(found.strings())) if found is not None else ""


def _clean(value: str) -> str:
    """Collapse whitespace, including the non-breaking spaces the site uses."""
    return " ".join(value.replace("\xa0", " ").split())


# ------------------------------------------------- BeautifulSoup, reproduced
#
# What 0.2.0's parser gets from BeautifulSoup(html, "html.parser") 4.x and
# its CSS selectors, rebuilt on the same tokenizer. The tree it builds is the
# same as bs4's in every way the parser can see:
#
# - An end tag closes the most recent open element of that name and every
#   element opened after it; an end tag with no open element is ignored.
#   Nothing is ever closed implicitly, except void elements such as <img>,
#   and anything written as <tag/>, which close at once.
# - Character references are resolved by bs4's rules, not html.unescape's.
# - Consecutive text is one string until a tag, comment or declaration ends
#   it. (bs4 also shortens whitespace-only strings, and keeps empty ones;
#   neither can change what _clean() makes of them, so neither is copied.)
# - get_text() only sees text, not comments, declarations or what is inside
#   <script>, <style>, <template>, <rt> or <rp>.
# - class is split on any Unicode whitespace and matched case-sensitively.

_VOID_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "basefont",
        "bgsound",
        "br",
        "col",
        "command",
        "embed",
        "frame",
        "hr",
        "image",
        "img",
        "input",
        "isindex",
        "keygen",
        "link",
        "menuitem",
        "meta",
        "nextid",
        "param",
        "source",
        "spacer",
        "track",
        "wbr",
    }
)
_STRING_CONTAINERS = frozenset({"rt", "rp", "style", "script", "template"})
_NON_WHITESPACE = re.compile(r"\S+")
_DECIMAL_REFERENCE = re.compile("^([0-9]+)(.*)")
_HEX_REFERENCE = re.compile("^([0-9a-f]+)(.*)")


def _entities() -> dict[str, str]:
    table: dict[str, str] = {}
    for name, character in sorted(html.entities.html5.items()):
        table.setdefault(name.removesuffix(";"), character)
    return table


_ENTITIES = _entities()


@dataclass(slots=True)
class _Text:
    #: "text" for page text, "cdata", or what else the string is: a comment,
    #: a doctype, or text inside one of the _STRING_CONTAINERS (its name).
    kind: str
    value: str


class _Element:
    __slots__ = ("attrs", "children", "classes", "name", "parent")

    def __init__(self, name: str, attrs: dict[str, str], parent: _Element | None) -> None:
        self.name = name
        self.attrs = attrs
        self.classes = _NON_WHITESPACE.findall(attrs["class"]) if "class" in attrs else []
        self.parent = parent
        self.children: list[_Element | _Text] = []

    def descendants(self) -> Iterator[_Element | _Text]:
        stack = list(reversed(self.children))
        while stack:
            node = stack.pop()
            yield node
            if isinstance(node, _Element):
                stack.extend(reversed(node.children))

    def select(self, name: str, cls: str | None = None) -> Iterator[_Element]:
        """Descendant elements matching the CSS selector ``name.cls``, in document order."""
        for node in self.descendants():
            if (
                isinstance(node, _Element)
                and node.name == name
                and (cls is None or cls in node.classes)
            ):
                yield node

    def has_ancestor(self, name: str, cls: str) -> bool:
        parent = self.parent
        while parent is not None:
            if parent.name == name and cls in parent.classes:
                return True
            parent = parent.parent
        return False

    def strings(self, *, strip: bool = False) -> Iterator[str]:
        """The text get_text() joins, of an element that is no string container."""
        for node in self.descendants():
            if isinstance(node, _Text) and node.kind in ("text", "cdata"):
                if not strip:
                    yield node.value
                elif value := node.value.strip():
                    yield value


class _HTMLTree(HTMLParser):
    """Builds the element tree, following bs4's ``BeautifulSoupHTMLParser``."""

    @classmethod
    def parse(cls, markup: str) -> _Element:
        builder = cls()
        builder.feed(markup)
        builder.close()
        builder._end_data()
        return builder.root

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.root = _Element("[document]", {}, None)
        self._stack = [self.root]
        self._open: Counter[str] = Counter()
        self._containers: list[_Element] = []
        self._closed_void: list[str] = []
        self._data: list[str] = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs, handle_empty_element=False)
        self.handle_endtag(tag, check_already_closed=False)

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]], handle_empty_element: bool = True
    ) -> None:
        # A repeated attribute keeps its last value; one without a value is "".
        attributes = {key: "" if value is None else value for key, value in attrs}
        self._end_data()
        element = _Element(tag, attributes, self._stack[-1])
        self._stack[-1].children.append(element)
        self._stack.append(element)
        self._open[tag] += 1
        if tag in _STRING_CONTAINERS:
            self._containers.append(element)
        if tag in _VOID_ELEMENTS and handle_empty_element:
            self.handle_endtag(tag, check_already_closed=False)
            self._closed_void.append(tag)

    def handle_endtag(self, tag: str, check_already_closed: bool = True) -> None:
        if check_already_closed and tag in self._closed_void:
            self._closed_void.remove(tag)
            return
        self._end_data()
        if tag == self.root.name:
            return
        for i in range(len(self._stack) - 1, 0, -1):
            if not self._open[tag]:
                break
            name = self._stack[i].name
            self._pop()
            if name == tag:
                break

    def _pop(self) -> None:
        element = self._stack.pop()
        self._open[element.name] -= 1
        if self._containers and element is self._containers[-1]:
            self._containers.pop()

    def handle_data(self, data: str) -> None:
        self._data.append(data)

    def handle_charref(self, name: str) -> None:
        base, pattern = 10, _DECIMAL_REFERENCE
        if name.startswith(("x", "X")):
            name, base, pattern = name[1:], 16, _HEX_REFERENCE
        number, extra = None, ""
        try:
            number = int(name, base)
        except ValueError:
            match = pattern.search(name)
            if match is not None:
                number, extra = int(match.group(1), base), match.group(2)
        if number is None:
            self.handle_data(name)
        else:
            self.handle_data(_numeric_reference(number) + extra)

    def handle_entityref(self, name: str) -> None:
        self.handle_data(_ENTITIES.get(name, f"&{name}"))

    def handle_comment(self, data: str) -> None:
        self._string(data, "comment")

    def handle_decl(self, decl: str) -> None:
        self._string(decl[len("DOCTYPE ") :], "doctype")

    def unknown_decl(self, data: str) -> None:
        if data.upper().startswith("CDATA["):
            self._string(data[len("CDATA[") :], "cdata")
        else:
            self._string(data, "declaration")

    def handle_pi(self, data: str) -> None:
        self._string(data, "pi")

    def _string(self, data: str, kind: str) -> None:
        self._end_data()
        self.handle_data(data)
        self._end_data(kind)

    def _end_data(self, kind: str = "text") -> None:
        if not self._data:
            return
        data = "".join(self._data)
        self._data = []
        if kind == "text" and self._containers:
            kind = self._containers[-1].name
        self._stack[-1].children.append(_Text(kind, data))


def _numeric_reference(number: int) -> str:
    """The character a numeric reference stands for, the way bs4 resolves it."""
    if number == 0 or number > 0x10FFFF or 0xD800 <= number <= 0xDFFF:
        return "�"
    if 0x80 <= number <= 0x9F:
        # Windows-1252 written as a reference; bytes it leaves undefined stay as is.
        try:
            return bytes([number]).decode("cp1252")
        except UnicodeDecodeError:
            pass
    return chr(number)


# ================================================================ migration

#: Schemas written by apigrunn-archiver 0.1.0.
SOURCE_SCHEMAS = (1, 2)

_REQUIRED_TABLES = {
    1: {"runs", "blobs", "snapshots"},
    2: {"runs", "blobs", "snapshots", "parsed_pages", "talk_cards"},
}

_SNAPSHOT_COLUMNS = (
    "run_id",
    "url",
    "final_url",
    "fetched_at",
    "status_code",
    "error",
    "headers",
    "etag",
    "last_modified",
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
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
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
    schema = source_schema(source)
    with tempfile.TemporaryDirectory(prefix="apigrunn-archiver-db-migrator-") as directory:
        tmp, result = _build(source, schema, Path(directory), drop_error_bodies=drop_error_bodies)
        _remove(tmp)
    return result


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
        _check_path(tmp)
    except BaseException as exc:
        recoverable = isinstance(exc, PathError) and exc.recoverable
        handle_exception(
            exc,
            opt_in=recoverable,
            mode="PATH",
            drop_error_bodies=drop_error_bodies,
        )
        if recoverable:
            _fix_path_error(tmp)

    try:
        # An empty file is an empty database, ready for 0.2.0's schema.
        _create_archive(tmp)
        with _read_only(source) as src, closing(_connect(tmp)) as dst:
            error_only = _copy(source, src, dst, drop_error_bodies=drop_error_bodies)

        with closing(_connect(tmp)) as out:
            parse_errors = _ensure_index(out)
            with _read_only(source) as src:
                problems = _verify(src, out, error_only, drop_error_bodies)
                mismatches = _compare_index(src, out) if schema >= 2 else []
                stats = {
                    table: out.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("runs", "snapshots", "blobs", "page_versions", "talk_versions")
                }
                tool_versions = tuple(
                    row[0]
                    for row in src.execute(
                        "SELECT DISTINCT tool_version FROM runs ORDER BY tool_version"
                    )
                )

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
        parse_errors=tuple(parse_errors),
        index_mismatches=tuple(mismatches),
    )


def _check_path(pth: Path):
    if any(p in str(pth) for p in _INVALID_SEPARATORS):
        raise PathError("Invalid path separators", pth, True)


def _fix_path_error(pth: Path):
    return str(pth).replace("\\", "/")


def _copy(
    source: Path, src: sqlite3.Connection, dst: sqlite3.Connection, *, drop_error_bodies: bool
) -> set[str]:
    """Copy runs, blobs and snapshots. Returns the blobs only error responses returned."""
    error_only = {row[0] for row in src.execute(_ERROR_ONLY_BLOBS)}
    skip = error_only if drop_error_bodies else set()
    columns = ", ".join(_SNAPSHOT_COLUMNS)

    try:
        with dst:
            dst.executemany(
                "INSERT INTO runs (id, started_at, finished_at, tool_version, status) "
                "VALUES (?, ?, ?, ?, ?)",
                map(
                    tuple,
                    src.execute(
                        "SELECT id, started_at, finished_at, tool_version, status FROM runs ORDER BY id"
                    ),
                ),
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
                map(
                    tuple,
                    src.execute(
                        f"""
                    SELECT {columns}, blob_sha256,
                           CASE WHEN error IS NULL THEN blob_sha256 END
                    FROM snapshots ORDER BY run_id, url
                    """
                    ),
                ),
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
    error_only: set[str],
    drop_error_bodies: bool,
) -> list[str]:
    """Check the migrated archive the way 0.2.0's ``verify`` does, and against the source."""
    problems: list[str] = []

    version = out.execute("PRAGMA user_version").fetchone()[0]
    if version != TARGET_SCHEMA:
        problems.append(f"schema version is {version}, not {TARGET_SCHEMA}")
    messages = [row[0] for row in out.execute("PRAGMA integrity_check")]
    if messages != ["ok"]:
        problems += [f"sqlite: {message}" for message in messages]

    for (sha,) in out.execute("SELECT sha256 FROM blobs ORDER BY sha256").fetchall():
        try:
            _blob(out, sha)
        except _IntegrityError as exc:
            problems.append(exc.message)
    problems += [
        f"run {run_id} {url} points at missing blob {sha}"
        for run_id, url, sha in out.execute(
            """
            SELECT s.run_id, s.url, s.blob_sha256 FROM snapshots s
            LEFT JOIN blobs b ON b.sha256 = s.blob_sha256
            WHERE s.blob_sha256 IS NOT NULL AND b.sha256 IS NULL
            ORDER BY s.run_id, s.url
            """
        )
    ]
    problems += [f"index {sha}: {reason}" for sha, reason in _bad_index_rows(out)]
    problems += [f"{url} ({sha[:12]}) was not parsed" for sha, url, _ in _unparsed_blobs(out)]

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
        tuple(row)
        for row in out.execute(f"SELECT {columns}, body_sha256, blob_sha256 FROM snapshots")
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


def _compare_index(src: sqlite3.Connection, out: sqlite3.Connection) -> list[tuple[str, str, str]]:
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
        new = [tuple(row[f] for f in _CARD_FIELDS) for row in _cards(out, page)]
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


# ============================================================== bug reports
#
# Only exceptions that are bugs are reported: refusals (MigratorError),
# failed verification and file system errors are handled by the CLI and never
# get here. Nothing is sent unless the user opts in, with --report-bugs or
# APIGRUNN_MIGRATOR_REPORT_BUGS=1, or by answering yes when asked. They are
# asked only when both stdin and stderr are a terminal.
#
# A report is anonymous: it holds versions, the platform, the mode the tool ran
# in, the exception's type, and the module, function and line of each frame of
# its traceback. It never holds the exception's message, file paths, URLs or
# anything else read from an archive, since those can identify the user.
#
# The library functions never report anything; only main() does.

REPORT_URL = "https://aigrunn-error-collection.jaap-a3f.workers.dev"

#: Set to ``1`` to send reports without asking, ``0`` to never send or ask.
REPORT_OPT_IN_ENV = "APIGRUNN_MIGRATOR_REPORT_BUGS"
#: Overrides :data:`REPORT_URL`, for testing.
REPORT_URL_ENV = "APIGRUNN_MIGRATOR_REPORT_URL"

_REPORT_PAYLOAD_VERSION = 1
_REPORT_MAX_FRAMES = 50
_REPORT_TIMEOUT = 5


def build_report(exc: BaseException, *, mode: str, drop_error_bodies: bool) -> dict[str, Any]:
    """The JSON payload describing ``exc``, with nothing that identifies the user."""
    frames = [
        {
            "module": frame.f_globals.get("__name__", "?"),
            "function": frame.f_code.co_name,
            "line": lineno,
        }
        for frame, lineno in traceback.walk_tb(exc.__traceback__)
    ]
    return {
        "payload_version": _REPORT_PAYLOAD_VERSION,
        "report_id": str(uuid.uuid4()),
        "tool": "apigrunn-archiver-db-migrator",
        "tool_version": __version__,
        "archiver_version": ARCHIVER_VERSION,
        "python": f"{platform.python_implementation()} {platform.python_version()}",
        "os": platform.system(),
        "machine": platform.machine(),
        "mode": mode,
        "drop_error_bodies": drop_error_bodies,
        "context": os.environ.get("AIGRUNN_SECRET_KEY"),
        "exception": {
            "type": _type_name(exc),
            "causes": [_type_name(c) for c in _chain(exc)],
            # The innermost frames are where it went wrong; keep those.
            "frames": frames[-_REPORT_MAX_FRAMES:],
        },
    }


def send_report(report: dict[str, Any], url: str | None = None) -> bool:
    """POST ``report`` as JSON; whether it was accepted. Never raises."""
    url = url or os.environ.get(REPORT_URL_ENV) or REPORT_URL
    request = urllib.request.Request(
        url,
        data=json.dumps(report).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": f"apigrunn-archiver-db-migrator/{__version__}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_REPORT_TIMEOUT) as response:
            return 200 <= response.status < 300
    except Exception:  # noqa: BLE001 - a failed report must not hide the crash
        return False


def handle_exception(
    exc: BaseException, *, opt_in: bool | None, mode: str, drop_error_bodies: bool
) -> None:
    """Report ``exc`` if the user opts in. ``opt_in`` is the ``--report-bugs`` flag.

    The traceback itself is printed by the caller, before this is called.
    """
    if opt_in is None:
        opt_in = _env_opt_in()
    if opt_in is False:
        return

    report = build_report(exc, mode=mode, drop_error_bodies=drop_error_bodies)
    if opt_in is None and not (_interactive() and _ask_to_report(report)):
        print(
            f"you can report this bug by running again with --report-bugs, "
            f"or set {REPORT_OPT_IN_ENV}=1",
            file=sys.stderr,
        )
        return

    if not send_report(report):
        print("could not send the bug report", file=sys.stderr)


def _env_opt_in() -> bool | None:
    value = os.environ.get(REPORT_OPT_IN_ENV, "").strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return None


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stderr.isatty()


def _ask_to_report(report: dict[str, Any]) -> bool:
    print(
        "\nThis is a bug in apigrunn-archiver-db-migrator. This anonymous report would be "
        "sent to its developers:\n",
        file=sys.stderr,
    )
    print(json.dumps(report, indent=2), file=sys.stderr)
    print(
        f"\nSend it? [y/N] (--report-bugs or {REPORT_OPT_IN_ENV}=1|0 skips this question) ",
        end="",
        file=sys.stderr,
        flush=True,
    )
    try:
        answer = sys.stdin.readline()
    except (KeyboardInterrupt, EOFError):
        print(file=sys.stderr)
        return False
    return answer.strip().lower() in ("y", "yes")


def _type_name(exc: BaseException) -> str:
    cls = type(exc)
    return (
        cls.__qualname__ if cls.__module__ == "builtins" else f"{cls.__module__}.{cls.__qualname__}"
    )


def _chain(exc: BaseException) -> list[BaseException]:
    """The exceptions ``exc`` was raised from or while handling, nearest first."""
    chain: list[BaseException] = []
    current = exc.__cause__ or exc.__context__
    while current is not None and current not in chain and len(chain) < 10:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


# ====================================================================== CLI

EXIT_VERIFY_FAILED = 1
EXIT_REFUSED = 2
EXIT_CRASHED = 3


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
        version=f"%(prog)s {__version__} (writes apigrunn-archiver {ARCHIVER_VERSION} archives)",
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
    mode.add_argument("--dry-run", action="store_true", help="migrate and verify, but keep nothing")
    parser.add_argument(
        "--drop-error-bodies",
        action="store_true",
        help="discard HTML returned only by error responses, which 0.2.0 does not keep "
        "(default: keep it)",
    )
    parser.add_argument(
        "--report-bugs",
        action=argparse.BooleanOptionalAction,
        help="if this tool crashes, send an anonymous bug report without asking "
        f"(--no-report-bugs: never send or ask; default: ${REPORT_OPT_IN_ENV}, "
        "else ask when run in a terminal)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.ERROR,
        format="%(levelname)s: %(message)s",
    )

    try:
        return _run(args)
    except Exception as exc:  # noqa: BLE001 - anything else is a bug
        traceback.print_exception(exc)
        print("error: apigrunn-archiver-db-migrator crashed; this is a bug", file=sys.stderr)

        handle_exception(
            exc,
            opt_in=args.report_bugs,
            mode=_mode(args),
            drop_error_bodies=args.drop_error_bodies,
        )
        return EXIT_CRASHED


def _mode(args: argparse.Namespace) -> str:
    if args.dry_run:
        return "dry-run"
    return "in-place" if args.in_place else "output"


def _run(args: argparse.Namespace) -> int:
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
        print(
            f"      to {result.output} (schema {TARGET_SCHEMA}, apigrunn-archiver {ARCHIVER_VERSION})"
        )
    else:
        print(
            f"      to schema {TARGET_SCHEMA} (apigrunn-archiver {ARCHIVER_VERSION}); "
            f"dry run, nothing written"
        )
    print(f"  {result.runs} runs, {result.snapshots} snapshots, {result.blobs} blobs")
    print(
        f"  talk index rebuilt: {result.page_versions} page versions, "
        f"{result.talk_versions} talk versions, {len(result.parse_errors)} unparseable pages"
    )
    print("  verified: every blob re-hashed, runs and snapshots match the source")
    if result.backup:
        print(f"  original kept as {result.backup}")
    if result.output and not result.backup:
        print(f"  use it with: apigrunn-archiver --db {result.output} runs")

    if result.error_bodies:
        if result.error_bodies_dropped:
            print(
                f"note: discarded {result.error_bodies} page bodies that only error "
                f"responses returned",
                file=sys.stderr,
            )
        else:
            print(
                f"note: kept {result.error_bodies} page bodies that only error responses "
                f"returned; 0.2.0 does not show them (--drop-error-bodies discards them)",
                file=sys.stderr,
            )
    for url, sha, message in result.parse_errors:
        print(
            f"warning: {url} ({sha[:12]}) could not be parsed and is kept as an "
            f"unparseable page: {message}",
            file=sys.stderr,
        )
    for url, sha, reason in result.index_mismatches:
        print(
            f"warning: {url} ({sha[:12]}) differs from the 0.1.0 talk index: {reason}",
            file=sys.stderr,
        )


if __name__ == "__main__":
    sys.exit(main())
