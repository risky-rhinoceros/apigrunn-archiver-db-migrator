from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing

import httpx
import pytest
from apigrunn_archiver import Archiver, Store
from apigrunn_archiver.targets import track_url

from apigrunn_archiver_db_migrator import (
    OutputExistsError,
    SourceError,
    UnsupportedArchiverError,
    VerificationError,
    backup_path,
    dry_run,
    migrate,
    migrate_in_place,
    source_schema,
)
from apigrunn_archiver_db_migrator import migration as migrate_module

from .conftest import add_noise, edit_cards, file_sha, first_video_id, fixture_bytes
from .legacy import LegacyArchive

BROKEN = b'<div class="talks-year-group"><a class="talk-card" href="/x"></a></div>'


def blob_shas(path) -> set[str]:
    with closing(sqlite3.connect(path)) as conn:
        return {row[0] for row in conn.execute("SELECT sha256 FROM blobs")}


def snapshot(path, run_id: int, track: str):
    with Archiver(db_path=path) as archiver:
        return next(s for s in archiver.snapshots(run_id) if s.url == track_url(track))


# ------------------------------------------------------------ real archive


def test_real_archive_migrates(real_archive, output):
    result = migrate(real_archive, output)

    assert result.source_schema == 2
    assert result.tool_versions == ("0.1.0",)
    assert (result.runs, result.snapshots, result.blobs) == (2, 16, 8)
    assert (result.page_versions, result.talk_versions) == (8, 66)
    assert result.parse_errors == result.index_mismatches == ()
    assert result.error_bodies == 0
    assert result.output == output and result.backup is None

    with Archiver(db_path=output) as archiver:
        assert archiver.verify().ok
        assert [(r.id, r.pages, r.changed) for r in archiver.runs()] == [(2, 8, 0), (1, 8, 8)]
        assert all(s.exact for run in (1, 2) for s in archiver.snapshots(run))
        assert len(archiver.talks()) == 66
        assert [e.kind for e in archiver.history("tech")] == ["first"]


def test_real_archive_talks_match_the_0_1_0_index(real_archive, output):
    # Read-only and immutable: a plain connection would leave a -wal file,
    # which the migrator rightly takes for an archive still in use.
    with closing(sqlite3.connect(f"file:{real_archive}?mode=ro&immutable=1", uri=True)) as conn:
        old = {row[0] for row in conn.execute("SELECT DISTINCT video_id FROM talk_cards")}
    migrate(real_archive, output)
    with Archiver(db_path=output) as archiver:
        assert {t.video_id for t in archiver.talks()} == old


def test_source_is_untouched(real_archive, output):
    before = file_sha(real_archive)
    migrate(real_archive, output)
    assert file_sha(real_archive) == before
    assert sorted(p.name for p in real_archive.parent.iterdir()) == ["archive.db", "migrated.db"]


def test_migrated_archive_keeps_working(real_archive, output):
    """A run by 0.2.0 after the migration reuses the migrated HTML."""
    migrate(real_archive, output)

    def serve(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=fixture_bytes("tech"),
                              headers={"Content-Type": "text/html; charset=utf-8"})

    with Archiver(db_path=output, targets=[track_url("tech")],
                  transport=httpx.MockTransport(serve)) as archiver:
        result = archiver.run()
        assert result.run_id == 3 and result.errors == 0
        assert archiver.verify().ok
        assert len(archiver.talks()) == 66


# ---------------------------------------------------------- what is mapped


def test_schema_1_archive_gets_a_talk_index(tmp_path, output):
    with LegacyArchive(tmp_path / "archive.db", schema=1) as legacy:
        run = legacy.run()
        legacy.snapshot(run, "tech", fixture_bytes("tech"))
        legacy.snapshot(run, "science", fixture_bytes("science"))

    result = migrate(legacy.path, output)
    assert result.source_schema == 1
    assert result.page_versions == 2 and result.talk_versions > 0
    assert result.index_mismatches == ()
    with Archiver(db_path=output) as archiver:
        assert archiver.verify().ok
        assert archiver.talks(track="science")


def test_runs_are_copied_as_is(legacy, output):
    legacy.snapshot(legacy.run(), "tech", fixture_bytes("tech"))
    legacy.run(status="running")
    legacy.close()

    migrate(legacy.path, output)
    with Archiver(db_path=output) as archiver:
        runs = {r.id: r for r in archiver.runs()}
    assert runs[1].status == "ok" and runs[1].tool_version == "0.1.0"
    assert runs[2].status == "running" and runs[2].finished_at is None


def test_error_response_keeps_its_hash_but_not_its_body(legacy, output):
    body = fixture_bytes("tech")
    legacy.snapshot(legacy.run(), "tech", body)
    legacy.snapshot(legacy.run(), "tech", b"try later", status=503, error="HTTP 503")
    legacy.close()

    result = migrate(legacy.path, output)
    assert result.error_bodies == 1 and not result.error_bodies_dropped

    failed = snapshot(output, 2, "tech")
    assert failed.blob_sha256 is None
    assert failed.body_sha256 == hashlib.sha256(b"try later").hexdigest()
    assert not failed.changed and not failed.exact
    assert failed.body_sha256 in blob_shas(output)  # kept by default
    with Archiver(db_path=output) as archiver:
        assert [e.kind for e in archiver.history("tech")] == ["first", "failed"]
        assert archiver.content("tech") == body


def test_drop_error_bodies(legacy, output):
    body = fixture_bytes("tech")
    legacy.snapshot(legacy.run(), "tech", body)
    legacy.snapshot(legacy.run(), "tech", b"try later", status=503, error="HTTP 503")
    # An error that returned a page some good snapshot also has: that body stays.
    legacy.snapshot(legacy.run(), "tech", body, status=500, error="HTTP 500")
    legacy.close()

    result = migrate(legacy.path, output, drop_error_bodies=True)
    assert result.error_bodies == 1 and result.error_bodies_dropped
    assert blob_shas(output) == {hashlib.sha256(body).hexdigest()}
    with Archiver(db_path=output) as archiver:
        assert archiver.verify().ok


def test_not_modified_points_at_the_previous_body(legacy, output):
    sha = legacy.snapshot(legacy.run(), "tech", fixture_bytes("tech"))
    legacy.snapshot(legacy.run(), "tech", None, not_modified=True)
    legacy.close()

    migrate(legacy.path, output)
    cached = snapshot(output, 2, "tech")
    assert cached.status_code == 304
    assert cached.blob_sha256 == cached.body_sha256 == sha
    assert cached.exact and not cached.changed


def test_noise_is_no_longer_a_change(legacy, output):
    body = fixture_bytes("tech")
    legacy.snapshot(legacy.run(), "tech", body)
    legacy.snapshot(legacy.run(), "tech", add_noise(body, "n2"))
    legacy.close()

    result = migrate(legacy.path, output)
    first, second = snapshot(output, 1, "tech"), snapshot(output, 2, "tech")
    assert first.changed and not second.changed
    assert first.page_sha256 == second.page_sha256
    assert first.blob_sha256 != second.blob_sha256
    assert first.exact and second.exact  # both bodies are kept
    assert result.blobs == 2 and result.page_versions == 1


def test_talk_edit_is_a_change(legacy, output):
    body = fixture_bytes("tech")
    legacy.snapshot(legacy.run(), "tech", body)
    legacy.snapshot(legacy.run(), "tech", edit_cards(body, first_video_id(body), title="Renamed"))
    legacy.close()

    migrate(legacy.path, output)
    assert snapshot(output, 2, "tech").changed
    with Archiver(db_path=output) as archiver:
        changes = archiver.talk_changes(1, 2)
    assert [after.title for _, after in changes.changed] == ["Renamed"]


def test_unparseable_page_is_migrated_with_its_html(legacy, output):
    legacy.snapshot(legacy.run(), "tech", fixture_bytes("tech"))
    legacy.snapshot(legacy.run(), "tech", BROKEN)
    legacy.close()

    result = migrate(legacy.path, output)
    assert [(url, sha) for url, sha, _ in result.parse_errors] == [
        (track_url("tech"), hashlib.sha256(BROKEN).hexdigest())
    ]
    assert result.index_mismatches == ()  # 0.1.0 could not parse it either
    assert snapshot(output, 2, "tech").page_sha256 is None
    with Archiver(db_path=output) as archiver:
        assert [e.kind for e in archiver.history("tech")] == ["first", "unparseable"]
        assert archiver.content("tech") == BROKEN


def test_disagreement_with_the_0_1_0_index_is_reported(legacy, output):
    sha = legacy.snapshot(legacy.run(), "tech", fixture_bytes("tech"))
    with legacy.conn:
        legacy.conn.execute("UPDATE talk_cards SET title = 'stale' WHERE position = 0")
    legacy.close()

    result = migrate(legacy.path, output)
    assert len(result.index_mismatches) == 1
    url, mismatched, reason = result.index_mismatches[0]
    assert (url, mismatched) == (track_url("tech"), sha)
    assert reason.startswith("1 of ") and reason.endswith(" talks have different fields")


# ------------------------------------------------------- in place, dry run


def test_in_place_keeps_the_original_as_backup(real_archive):
    before = file_sha(real_archive)
    result = migrate_in_place(real_archive)

    backup = real_archive.with_name("archive.db.schema2.bak")
    assert result.backup == backup == backup_path(real_archive, 2)
    assert result.output == real_archive
    assert file_sha(backup) == before
    assert source_schema(backup) == 2
    with Archiver(db_path=real_archive) as archiver:
        assert archiver.verify().ok
    assert sorted(p.name for p in real_archive.parent.iterdir()) == [
        "archive.db", "archive.db.schema2.bak"
    ]


def test_without_hard_links_files_are_copied(real_archive, output, monkeypatch):
    def no_links(src, dst):
        raise OSError("hard links not supported")

    monkeypatch.setattr(migrate_module.os, "link", no_links)
    before = file_sha(real_archive)
    migrate(real_archive, output)
    result = migrate_in_place(real_archive)

    assert file_sha(result.backup) == before
    with Archiver(db_path=output) as a, Archiver(db_path=real_archive) as b:
        assert a.verify().ok and b.verify().ok
    assert sorted(p.name for p in real_archive.parent.iterdir()) == [
        "archive.db", "archive.db.schema2.bak", "migrated.db"
    ]


def test_in_place_refuses_to_overwrite_a_backup(real_archive):
    backup = backup_path(real_archive, 2)
    backup.write_bytes(b"older backup")
    before = file_sha(real_archive)

    with pytest.raises(OutputExistsError, match=r"MIGRATOR-E200.*backup"):
        migrate_in_place(real_archive)
    assert file_sha(real_archive) == before
    assert backup.read_bytes() == b"older backup"


def test_dry_run_writes_nothing(real_archive):
    before = sorted(real_archive.parent.iterdir())
    result = dry_run(real_archive)
    assert result.output is None and result.snapshots == 16
    assert sorted(real_archive.parent.iterdir()) == before


def test_output_is_never_overwritten(real_archive, output):
    output.write_bytes(b"something else")
    with pytest.raises(OutputExistsError, match=r"MIGRATOR-E200"):
        migrate(real_archive, output)
    assert output.read_bytes() == b"something else"


# -------------------------------------------------------------- refusals


def test_missing_source(tmp_path, output):
    with pytest.raises(SourceError, match=r"MIGRATOR-E100.*does not exist"):
        migrate(tmp_path / "nope.db", output)


def test_source_in_use_is_refused(legacy, output):
    legacy.snapshot(legacy.run(), "tech", fixture_bytes("tech"))
    # Still open, so its write-ahead log exists.
    with pytest.raises(SourceError, match=r"archive.db-wal exists"):
        migrate(legacy.path, output)
    assert not output.exists()


def test_current_archive_is_refused(tmp_path, output):
    Store(tmp_path / "archive.db").close()
    with pytest.raises(SourceError, match=r"already at schema 3"):
        migrate(tmp_path / "archive.db", output)


def test_empty_file_is_refused(tmp_path, output):
    (tmp_path / "archive.db").touch()
    with pytest.raises(SourceError, match=r"an empty database"):
        migrate(tmp_path / "archive.db", output)


def test_non_sqlite_file_is_refused(tmp_path, output):
    (tmp_path / "archive.db").write_bytes(b"not a database at all, just some text" * 50)
    with pytest.raises(SourceError, match=r"not an SQLite database"):
        migrate(tmp_path / "archive.db", output)


def test_unknown_schema_is_refused(tmp_path, output):
    with closing(sqlite3.connect(tmp_path / "archive.db")) as conn:
        conn.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY)")
        conn.execute("PRAGMA user_version = 7")
    with pytest.raises(SourceError, match=r"at schema 7"):
        migrate(tmp_path / "archive.db", output)


def test_incomplete_schema_is_refused(tmp_path, output):
    with LegacyArchive(tmp_path / "archive.db", schema=2) as legacy:
        legacy.conn.execute("DROP TABLE talk_cards")
    with pytest.raises(SourceError, match=r"no talk_cards table"):
        migrate(legacy.path, output)


def test_other_archiver_schema_is_refused(real_archive, output, monkeypatch):
    monkeypatch.setattr(migrate_module, "SCHEMA_VERSION", 4)
    with pytest.raises(UnsupportedArchiverError, match=r"MIGRATOR-E400"):
        migrate(real_archive, output)


# ---------------------------------------------------------- verification


def test_damaged_source_blob_fails_verification(legacy, output):
    legacy.snapshot(legacy.run(), "tech", fixture_bytes("tech"))
    with legacy.conn:
        legacy.conn.execute("UPDATE blobs SET compression = 'none', data = x'00'")
    legacy.close()

    with pytest.raises(VerificationError, match=r"MIGRATOR-E300") as info:
        migrate(legacy.path, output)
    assert any("does not match its hash" in p for p in info.value.problems)
    assert not output.exists()
    assert sorted(p.name for p in output.parent.iterdir()) == ["archive.db"]


def test_failed_verification_leaves_nothing_behind(real_archive, output, monkeypatch):
    monkeypatch.setattr(migrate_module, "_verify", lambda *args: ["boom"])
    before = file_sha(real_archive)

    with pytest.raises(VerificationError, match=r"boom"):
        migrate_in_place(real_archive)
    with pytest.raises(VerificationError):
        migrate(real_archive, output)

    assert file_sha(real_archive) == before
    assert sorted(p.name for p in real_archive.parent.iterdir()) == ["archive.db"]
