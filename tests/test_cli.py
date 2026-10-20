from __future__ import annotations

import pytest

from apigrunn_archiver_db_migrator import migration as migrate_module
from apigrunn_archiver_db_migrator import source_schema
from apigrunn_archiver_db_migrator.cli import main

from .conftest import fixture_bytes


def test_migrate_to_output(real_archive, output, capsys):
    assert main([str(real_archive), "-o", str(output)]) == 0
    out = capsys.readouterr().out
    assert f"migrated {real_archive} (schema 2, apigrunn-archiver 0.1.0)" in out
    assert f"to {output} (schema 3" in out
    assert "2 runs, 16 snapshots, 8 blobs" in out
    assert "8 page versions, 66 talk versions, 0 unparseable pages" in out
    assert f"use it with: apigrunn-archiver --db {output} runs" in out
    assert source_schema(real_archive) == 2


def test_in_place(real_archive, capsys):
    assert main([str(real_archive), "--in-place"]) == 0
    assert f"original kept as {real_archive}.schema2.bak" in capsys.readouterr().out


def test_dry_run(real_archive, capsys):
    assert main([str(real_archive), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would migrate" in out and "nothing written" in out
    assert [p.name for p in real_archive.parent.iterdir()] == ["archive.db"]


def test_source_defaults_to_the_archivers_archive(real_archive, monkeypatch, capsys):
    monkeypatch.setenv("APIGRUNN_ARCHIVE", str(real_archive))
    assert main(["--dry-run"]) == 0
    assert f"would migrate {real_archive}" in capsys.readouterr().out


def test_a_mode_is_required(real_archive, capsys):
    with pytest.raises(SystemExit) as info:
        main([str(real_archive)])
    assert info.value.code == 2
    assert "one of the arguments -o/--output --in-place --dry-run is required" in (
        capsys.readouterr().err
    )


def test_refusal_exits_2(tmp_path, capsys):
    assert main([str(tmp_path / "nope.db"), "--dry-run"]) == 2
    assert "[MIGRATOR-E100]" in capsys.readouterr().err


def test_existing_output_exits_2(real_archive, output, capsys):
    output.touch()
    assert main([str(real_archive), "-o", str(output)]) == 2
    assert "[MIGRATOR-E200]" in capsys.readouterr().err


def test_missing_output_directory_exits_2(real_archive, tmp_path, capsys):
    assert main([str(real_archive), "-o", str(tmp_path / "no" / "such" / "dir.db")]) == 2
    assert "nothing was written" in capsys.readouterr().err


def test_failed_verification_exits_1(real_archive, output, monkeypatch, capsys):
    monkeypatch.setattr(migrate_module, "_verify", lambda *args: ["boom"])
    assert main([str(real_archive), "-o", str(output)]) == 1
    err = capsys.readouterr().err
    assert "[MIGRATOR-E300]" in err and "- boom" in err
    assert not output.exists()


def test_warnings_go_to_stderr(legacy, output, capsys):
    legacy.snapshot(legacy.run(), "tech", fixture_bytes("tech"))
    run = legacy.run()
    legacy.snapshot(run, "tech", b"try later", status=503, error="HTTP 503")
    legacy.snapshot(run, "science", b'<div class="talks-year-group"><a class="talk-card"></a></div>')
    legacy.close()

    assert main([str(legacy.path), "-o", str(output)]) == 0
    captured = capsys.readouterr()
    assert "note: kept 1 page bodies that only error responses returned" in captured.err
    assert "warning: https://www.aigrunn.org/science" in captured.err
    assert "could not be parsed" in captured.err
    assert "warning:" not in captured.out


def test_drop_error_bodies_flag(legacy, output, capsys):
    legacy.snapshot(legacy.run(), "tech", b"try later", status=503, error="HTTP 503")
    legacy.close()

    assert main([str(legacy.path), "-o", str(output), "--drop-error-bodies"]) == 0
    assert "note: discarded 1 page bodies" in capsys.readouterr().err
