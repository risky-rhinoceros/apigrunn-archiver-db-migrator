from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from .legacy import LegacyArchive

FIXTURES = Path(__file__).parent / "fixtures"

#: Written by the real apigrunn-archiver 0.1.0: two runs of all eight track
#: pages, byte-identical between the runs.
REAL_ARCHIVE = FIXTURES / "archive_v0.1.0.db"


def fixture_bytes(track: str) -> bytes:
    return (FIXTURES / f"{track}.html").read_bytes()


def edit_cards(html: bytes, video_id: str, *, remove: bool = False, **attrs: str) -> bytes:
    """Remove, or change ``data-*`` attributes of, the talk card for ``video_id``."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    anchors = [a for a in soup.select("a.talk-card") if video_id in (a.get("href") or "")]
    assert anchors, f"no card for {video_id}"
    for anchor in anchors:
        if remove:
            anchor.decompose()
        else:
            for key, value in attrs.items():
                anchor[f"data-{key}"] = value
    return str(soup).encode("utf-8")


def first_video_id(html: bytes) -> str:
    from bs4 import BeautifulSoup

    href = BeautifulSoup(html, "html.parser").select_one("a.talk-card")["href"]
    return href.split("v=")[1].split("&")[0]


def add_noise(html: bytes, token: str) -> bytes:
    """Change the page the way a CMS does on every request: same talks, new bytes."""
    return html.replace(b"</body>", f'<script nonce="{token}"></script></body>'.encode(), 1)


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def real_archive(tmp_path: Path) -> Path:
    """A private copy of the real 0.1.0 archive."""
    path = tmp_path / "archive.db"
    shutil.copyfile(REAL_ARCHIVE, path)
    return path


@pytest.fixture
def legacy(tmp_path: Path):
    """Build a schema 2 archive with ``legacy.run()`` and ``legacy.snapshot()``."""
    archive = LegacyArchive(tmp_path / "archive.db", schema=2)
    yield archive
    archive.close()


@pytest.fixture
def output(tmp_path: Path) -> Path:
    return tmp_path / "migrated.db"
