"""The copy of apigrunn-archiver 0.2.0 inside the script against the real 0.2.0.

The script carries 0.2.0's schema, hashes and talk parser so that it needs
nothing installed. These tests hold each of them to the installed 0.2.0:

- the pin: the installed archiver is the version the copy was taken from;
- the schema, created by both, compared in ``sqlite_master``;
- whole migrations, whose talk index is rebuilt by 0.2.0's own
  ``ensure_index`` and compared with the script's, every table row by row;
- the parser, on the real pages, on edge cases picked by hand, and on
  thousands of generated and mutated pages, compared card by card.
"""

from __future__ import annotations

import dataclasses
import gzip
import random
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import apigrunn_archiver
import pytest
from apigrunn_archiver import store as archiver_store
from apigrunn_archiver.index import ensure_index, parse_page
from apigrunn_archiver.parser import PARSER_VERSION
from apigrunn_archiver.targets import track_url
from bs4.builder import HTMLTreeBuilder

import apigrunn_archiver_db_migrator as migrator

from .conftest import REAL_ARCHIVE, add_noise, edit_cards, first_video_id, fixture_bytes
from .legacy import CONTENT_TYPE, LegacyArchive

pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")  # bs4 on odd markup

URL = track_url("tech")
#: What bs4 builds its tree around, rather than what the script thinks it does.
VOID_ELEMENTS = sorted(HTMLTreeBuilder.DEFAULT_EMPTY_ELEMENT_TAGS)


# -------------------------------------------------------------- the pin


def test_installed_archiver_is_the_one_copied():
    assert apigrunn_archiver.__version__ == migrator.ARCHIVER_VERSION
    assert apigrunn_archiver.SCHEMA_VERSION == migrator.TARGET_SCHEMA
    assert PARSER_VERSION == migrator.PARSER_VERSION
    assert archiver_store.SCHEMA == migrator._SCHEMA
    assert archiver_store._TALK_FIELDS == migrator._TALK_FIELDS


@pytest.mark.parametrize("env", [
    {},
    {"APIGRUNN_ARCHIVE": "~/elsewhere.db"},
    {"XDG_DATA_HOME": "/data"},
    {"APIGRUNN_ARCHIVE": "", "XDG_DATA_HOME": "~/data"},
])
def test_default_db_path(monkeypatch, env):
    monkeypatch.delenv("APIGRUNN_ARCHIVE", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert migrator.default_db_path() == apigrunn_archiver.default_db_path()


# ------------------------------------------------------------ the schema


def schema_of(path: Path) -> tuple:
    with closing(sqlite3.connect(path)) as conn:
        return (
            conn.execute("PRAGMA user_version").fetchone()[0],
            conn.execute("PRAGMA journal_mode").fetchone()[0],
            sorted(conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master")),
        )


def test_schema_is_the_one_0_2_0_creates(tmp_path):
    archiver_store.Store(tmp_path / "real.db").close()
    migrator._create_archive(tmp_path / "copy.db")
    assert schema_of(tmp_path / "copy.db") == schema_of(tmp_path / "real.db")


# ---------------------------------------------------- whole migrations

INDEX_TABLES = ("talk_versions", "page_versions", "page_talks", "blob_parses")


def dump(path: Path) -> dict[str, list[tuple]]:
    """Every row of every table, in a stable order."""
    with closing(sqlite3.connect(path)) as conn:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )]
        return {t: sorted(map(tuple, conn.execute(f"SELECT * FROM {t}")), key=repr) for t in tables}


def assert_index_is_0_2_0s(output: Path, result: migrator.MigrationResult, tmp_path: Path):
    """Rebuild the index of ``output`` with 0.2.0 itself; it must come out the same."""
    reference = tmp_path / "reference.db"
    shutil.copyfile(output, reference)
    store = archiver_store.Store(reference)
    try:
        store.clear_index()
        assert dump_index(reference) == {t: [] for t in INDEX_TABLES}
        index = ensure_index(store)
    finally:
        store.close()
    assert dump(output) == dump(reference)
    assert list(result.parse_errors) == index.errors


def dump_index(path: Path) -> dict[str, list[tuple]]:
    return {t: rows for t, rows in dump(path).items() if t in INDEX_TABLES}


def build(legacy: LegacyArchive, *steps) -> Path:
    for step in steps:
        step(legacy)
    legacy.close()
    return legacy.path


def set_content_type(content_type: str):
    def step(legacy: LegacyArchive):
        with legacy.conn:
            legacy.conn.execute(
                "UPDATE snapshots SET headers = json_array(json_array('Content-Type', ?))",
                (content_type,),
            )
    return step


def history(*bodies: bytes | None):
    def step(legacy: LegacyArchive):
        # 0.1.0 indexed each (blob, url) once; LegacyArchive cannot do it twice.
        indexed: set[bytes] = set()
        for i, body in enumerate(bodies):
            run = legacy.run()
            if i == 0:
                legacy.snapshot(run, "science", fixture_bytes("science"))
            if body is None:
                legacy.snapshot(run, "tech", b"try later", status=503, error="HTTP 503")
            elif body in indexed:
                legacy.snapshot(run, "tech", None, not_modified=True)
            else:
                indexed.add(body)
                legacy.snapshot(run, "tech", body)
    return step


TECH = fixture_bytes("tech")
SCENARIOS = {
    "plain": [history(TECH)],
    "noise, errors, edits": [history(
        TECH, add_noise(TECH, "n1"), None,
        edit_cards(TECH, first_video_id(TECH), title="Renamed", speaker=""),
        edit_cards(TECH, first_video_id(TECH), remove=True),
        TECH,
    )],
    "unparseable": [history(
        TECH, b'<div class="talks-year-group"><a class="talk-card" href="/x"></a></div>',
        b"<html>no talks here</html>", b"", b"talk-card",
    )],
    "latin-1": [history(TECH.replace(b"</body>", "<p>caf\xe9</p></body>".encode("latin-1"))),
                set_content_type("text/html; charset=ISO-8859-1")],
    "unknown charset": [history(TECH), set_content_type("text/html; charset=bogus-9")],
    "no content type": [history(TECH), set_content_type("text/html")],
}


@pytest.mark.parametrize("drop_error_bodies", [False, True])
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_migration_index_is_0_2_0s(tmp_path, scenario, drop_error_bodies):
    source = build(LegacyArchive(tmp_path / "archive.db", schema=2), *SCENARIOS[scenario])
    output = tmp_path / "migrated.db"
    result = migrator.migrate(source, output, drop_error_bodies=drop_error_bodies)
    assert_index_is_0_2_0s(output, result, tmp_path)


def test_real_archive_index_is_0_2_0s(real_archive, output, tmp_path):
    result = migrator.migrate(real_archive, output)
    assert_index_is_0_2_0s(output, result, tmp_path)


def test_schema_1_index_is_0_2_0s(tmp_path):
    source = build(LegacyArchive(tmp_path / "archive.db", schema=1), history(TECH, TECH[:5000]))
    output = tmp_path / "migrated.db"
    assert_index_is_0_2_0s(output, migrator.migrate(source, output), tmp_path)


# ------------------------------------------------------------- the parser


def both(html: str | bytes, content_type: str = CONTENT_TYPE, url: str = URL):
    raw = html.encode("utf-8") if isinstance(html, str) else html
    expected_cards, expected_error = parse_page(raw, content_type, url)
    cards, error = migrator._parse_page(raw, content_type, url)
    return (
        ([dataclasses.astuple(c) for c in cards], error),
        ([dataclasses.astuple(c) for c in expected_cards], expected_error),
    )


def assert_same_parse(html: str | bytes, content_type: str = CONTENT_TYPE, url: str = URL):
    got, expected = both(html, content_type, url)
    assert got == expected, html if isinstance(html, str) else html[:2000]


def real_pages() -> list[tuple[str, bytes]]:
    with closing(sqlite3.connect(f"file:{REAL_ARCHIVE}?mode=ro&immutable=1", uri=True)) as conn:
        rows = conn.execute(
            "SELECT DISTINCT s.url, b.data FROM snapshots s JOIN blobs b ON b.sha256 = s.blob_sha256"
        ).fetchall()
    return [(url, gzip.decompress(data)) for url, data in rows]


@pytest.mark.parametrize(("url", "raw"), real_pages(), ids=lambda v: v if isinstance(v, str) else "")
def test_real_pages(url, raw):
    assert_same_parse(raw, url=url)


@pytest.mark.parametrize("track", ["tech", "science"])
def test_fixture_pages(track):
    got, expected = both(fixture_bytes(track))
    assert got == expected and got[0]


def card(inner: str = "", **attrs: str | None) -> str:
    attrs = {"class": "talk-card", "href": "https://www.youtube.com/watch?v=abc123"} | attrs
    rendered = " ".join(k if v is None else f'{k}="{v}"' for k, v in attrs.items())
    return f"<a {rendered}>{inner}</a>"


def group(*cards: str, badge: str = '<span class="talks-year-badge y2025">2025</span>') -> str:
    return f'<div class="talks-year-group">{badge}<div class="talks-lane">{"".join(cards)}</div></div>'


EDGE_CASES = {
    "minimal": group(card(**{"data-title": "T"})),
    "fallback divs": group(card(
        '<div class="talk-title"> A <b>bold</b>\n title </div><div class="talk-speaker">S</div>'
        '<div class="talk-desc">D<!-- comment -->E</div>'
    )),
    "empty attribute wins": group(card('<div class="talk-speaker">aiGrunn 2024</div>',
                                       **{"data-title": "T", "data-speaker": ""})),
    "valueless attribute": group(card(**{"data-title": "T", "data-speaker": None})),
    "duplicate attribute": '<div class="talks-year-group"><a class="talk-card" href="x" '
                           'href="https://youtu.be/dup" data-title="a" data-title="b"></a></div>',
    "entities in attributes": group(card(**{"data-title": "Fish &amp; chips &eacute; &notin &bogus;",
                                            "href": "https://www.youtube.com/watch?v=a&amp;t=1"})),
    "entities in text": group(card(
        '<div class="talk-title">&amp; &amp &notin; &notit; &bogus; &#65;&#x42;&#X43; &#150; '
        '&#0; &#xD800; &#1114112; &#129; &#x9f; &#xfdd0; &#65abc; &#x4g; &nbsp;x&#160;y</div>'
    )),
    "cdata and script in text": group(card(
        '<div class="talk-title">a<![CDATA[b]]>c<script>d</script><style>e</style>'
        '<template>f</template><ruby>g<rt>h</rt><rp>i</rp></ruby></div>'
    )),
    "year from text": group(card(**{"data-title": "T"}),
                            badge='<span class="talks-year-badge">Year <b>20</b>24!</span>'),
    "year split by comment": group(card(**{"data-title": "T"}),
                                   badge='<span class="talks-year-badge">20<!-- -->24</span>'),
    "year class not exact": group(card(**{"data-title": "T"}),
                                  badge='<span class="talks-year-badge y20245 xy2023">1999</span>'),
    "no badge": group(card(**{"data-title": "T"}), badge=""),
    "nbsp splits classes": '<div class="x\xa0talks-year-group">' + card(**{"data-title": "T"}) + "</div>",
    "classes are case-sensitive": '<div class="Talks-Year-Group">' + card(**{"data-title": "T"}) + "</div>",
    "uppercase tags": '<DIV CLASS="talks-year-group"><A CLASS="talk-card" HREF="https://youtu.be/x" '
                      'DATA-TITLE="T"></A></DIV>',
    "nested groups": '<div class="talks-year-group"><span class="talks-year-badge y2020"></span>'
                     + card(**{"data-title": "outer"}) + group(card(**{"data-title": "inner",
                     "href": "https://youtu.be/inner"}), badge='<span class="talks-year-badge y2021"></span>')
                     + "</div>",
    "duplicate video": group(card(**{"data-title": "1"}), card(**{"data-title": "2"})),
    "card outside group": group(card(**{"data-title": "in"}))
                          + card(**{"data-title": "out", "href": "https://youtu.be/out"}),
    "only cards outside groups": card(**{"data-title": "out"}),
    "self-closing div": '<div class="talks-year-group"/>' + card(**{"data-title": "T"}),
    "unclosed group": '<div class="talks-year-group">' + card(**{"data-title": "T"}),
    "stray end tags": '</div></a><div class="talks-year-group"></span>'
                      + card(**{"data-title": "T"}) + "</div></div>",
    "end tag closes inner": '<div class="talks-year-group"><a class="talk-card" href="https://youtu.be/a">'
                            '<div class="talk-title">T</a><b>after</b></div>',
    "stray void end tags": "<img><br><spacer>" + group(card(
        '<div class="talk-title">a</img>b</br>c</spacer>d</img>e</div>')) + "</spacer>",
    "void end tag closes": '<div class="talks-year-group"></spacer>'
                           + card(**{"data-title": "T"}) + "</div>",
    "void end tag": group(card('<div class="talk-thumb"><img src=" s.jpg "></img></div>',
                               **{"data-title": "T"})),
    "thumb outside card": '<div class="talk-thumb">' + group(card('<img src="s">', **{"data-title": "T"}))
                          + "</div>",
    "thumb not first img": group(card('<img src="first"><div class="talk-thumb"><img src=""></div>',
                                      **{"data-title": "T"})),
    "pre keeps whitespace": group(card('<pre><div class="talk-title">  \n </div></pre>')),
    "template content": "<template>" + group(card(**{"data-title": "T"})) + "</template>",
    "svg and math": "<svg>" + group(card(**{"data-title": "T"})) + "</svg><math><a class=talk-card></a></math>",
    "script holding cards": "<script>" + group(card(**{"data-title": "T"})) + "</script>",
    "comment holding cards": "<!--" + group(card(**{"data-title": "T"})) + "-->",
    "doctype and pi": "<!DOCTYPE html><?xml version='1.0'?>" + group(card(**{"data-title": "T"})),
    "bogus declarations": "<!ELEMENT x><![if IE]>" + group(card(**{"data-title": "T"})) + "<![endif]>",
    "text mentions talk-card": "<p>no talk-card here</p>",
    "nothing": "",
    "youtube urls": group(*(card(**{"data-title": "T", "href": href}) for href in [
        "https://youtu.be/", "https://youtu.be/a/b", "https://m.youtube.com/embed/e1",
        "https://music.youtube.com/watch?x=1&v=m1&v=m2", "https://YOUTUBE.com/shorts/s1",
        "https://www.youtube.com/v/", "https://vimeo.com/watch?v=no", "  https://youtu.be/sp  ",
        "//youtu.be/rel", "https://www.youtube.com/watch", "https://www.youtube.com/embed",
    ])),
}


@pytest.mark.parametrize("name", EDGE_CASES)
def test_edge_cases(name):
    assert_same_parse(EDGE_CASES[name])


def test_tree_constants_are_bs4s():
    assert migrator._VOID_ELEMENTS == HTMLTreeBuilder.DEFAULT_EMPTY_ELEMENT_TAGS
    assert migrator._STRING_CONTAINERS == set(HTMLTreeBuilder.DEFAULT_STRING_CONTAINERS)


@pytest.mark.parametrize("tag", VOID_ELEMENTS)
def test_void_elements(tag):
    """A void element never stays open, so its end tag cannot close the group."""
    assert_same_parse(f'<{tag}><div class="talks-year-group"></{tag}>'
                      + card(**{"data-title": "T"}) + "</div>")


@pytest.mark.parametrize("start", range(0, 0x110000 + 0x2000, 0x2000))
def test_every_kind_of_character_reference(start):
    """A sample of numeric references across all of Unicode, all of 0-0x2ff."""
    numbers = list(range(start, start + 0x300)) if start == 0 else [start, start + 1, start - 1]
    title = "".join(f"&#{n};&#x{n:x};" for n in numbers if n >= 0)
    assert_same_parse(group(card(f'<div class="talk-title">{title}</div>')))


# Pieces random pages are made of: everything the parser looks at, and
# everything that changes how the tree is built around it.
CLASSES = [
    "talks-year-group", "talks-year-group", "talk-card", "talk-card", "talks-year-badge",
    "y2024", "y1999", "y20245", "talk-thumb", "talk-title", "talk-speaker", "talk-desc",
    "talks-lane", "Talk-Card", "talk-card\xa0x", "",
]
TAGS = ["div", "div", "a", "a", "span", "img", "br", "p", "b", "script", "style", "template",
        "rt", "pre", "textarea", "svg", "table", "tr", "td", "DIV", "A", "IMG",
        *VOID_ELEMENTS]
HREFS = [
    "https://www.youtube.com/watch?v=v1", "https://www.youtube.com/watch?v=v2&amp;t=3",
    "https://youtu.be/v3", "https://youtu.be/v1", "https://www.youtube.com/embed/v4",
    "https://www.youtube.com/shorts/", "https://example.com/v5", "", " https://youtu.be/v6 ",
]
TEXTS = [
    "", " ", "\n  ", "2023", "Talk &amp; more", "&nbsp;", "\xa0", "caf&eacute;", "&#150;",
    "&#x1F600;", "&bogus", "a&b", "talk-card", "<!-- c -->", "<![CDATA[x]]>", "&#0;", "&lt;",
    "Year 20<b>25</b>", "\t\r\n", " ", "&#13;", "ok",
]


def random_attrs(rng: random.Random) -> str:
    attrs = []
    if rng.random() < 0.7:
        attrs.append(f'class="{" ".join(rng.sample(CLASSES, rng.randint(1, 3)))}"')
    if rng.random() < 0.5:
        attrs.append(f'href="{rng.choice(HREFS)}"')
    for name in ("data-title", "data-speaker", "data-desc"):
        roll = rng.random()
        if roll < 0.3:
            attrs.append(f'{name}="{rng.choice(TEXTS[:13])}"')
        elif roll < 0.35:
            attrs.append(name)
    if rng.random() < 0.2:
        attrs.append(f'src="{rng.choice(HREFS)}"')
    if rng.random() < 0.05:
        attrs.append('class="talk-card"')
    return " ".join(attrs)


def random_html(rng: random.Random, depth: int = 0) -> str:
    out = []
    for _ in range(rng.randint(0, 6 if depth < 4 else 1)):
        roll = rng.random()
        if roll < 0.3:
            out.append(rng.choice(TEXTS))
        elif roll < 0.85 and depth < 6:
            tag = rng.choice(TAGS)
            style = rng.random()
            if style < 0.08:
                out.append(f"<{tag} {random_attrs(rng)}/>")
            else:
                inner = random_html(rng, depth + 1)
                close = "" if style < 0.18 else f"</{rng.choice([tag, tag, rng.choice(TAGS)])}>"
                out.append(f"<{tag} {random_attrs(rng)}>{inner}{close}")
        elif roll < 0.92:
            out.append(f"</{rng.choice(TAGS)}>")
        else:
            out.append(rng.choice(["<!DOCTYPE html>", "<?pi x?>", "<!x>", "<!--", "<", "&#", "<a"]))
    return "".join(out)


@pytest.mark.parametrize("batch", range(20))
def test_random_pages(batch):
    rng = random.Random(batch)
    for _ in range(250):
        assert_same_parse(random_html(rng))


@pytest.mark.parametrize("batch", range(10))
def test_mutated_real_pages(batch):
    """Real pages with pieces cut out, duplicated and swapped."""
    rng = random.Random(1000 + batch)
    page = fixture_bytes(rng.choice(["tech", "science"])).decode("utf-8")
    for _ in range(20):
        mutated = page
        for _ in range(rng.randint(1, 8)):
            i, j = sorted(rng.randrange(len(mutated) + 1) for _ in range(2))
            j = min(j, i + rng.choice([1, 5, 50, 500]))
            k = rng.randrange(len(mutated) + 1)
            mutated = rng.choice([
                mutated[:i] + mutated[j:],
                mutated[:k] + mutated[i:j] + mutated[k:],
                mutated[:i] + rng.choice(TEXTS + ["</div>", "</a>", "<div>", "<a>"]) + mutated[i:],
            ])
        assert_same_parse(mutated)
