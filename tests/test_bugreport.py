from __future__ import annotations

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from apigrunn_archiver_db_migrator import bugreport
from apigrunn_archiver_db_migrator import migration as migrate_module
from apigrunn_archiver_db_migrator.cli import EXIT_CRASHED, main

SECRET = "message-that-must-not-be-sent"


@pytest.fixture
def endpoint(monkeypatch):
    """A local endpoint, which the reports go to; its list fills with what it receives."""
    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            received.append(json.loads(self.rfile.read(length)))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(bugreport.URL_ENV, f"http://127.0.0.1:{server.server_port}/")
    monkeypatch.delenv(bugreport.OPT_IN_ENV, raising=False)
    yield received
    server.shutdown()
    server.server_close()


@pytest.fixture
def crash(monkeypatch):
    """Make the migration crash with a bug, deep inside it."""
    def verify(*args):
        try:
            {}["missing"]
        except KeyError as exc:
            raise RuntimeError(SECRET) from exc

    monkeypatch.setattr(migrate_module, "_verify", verify)


@pytest.fixture
def terminal(monkeypatch):
    """Run as if in a terminal, answering the question with whatever is set on it."""
    class Terminal:
        answer = ""
        asked = False

    def ask(report):
        Terminal.asked = True
        monkeypatch.setattr("sys.stdin", io.StringIO(Terminal.answer))
        return original_ask(report)

    original_ask = bugreport._ask
    monkeypatch.setattr(bugreport, "_interactive", lambda: True)
    monkeypatch.setattr(bugreport, "_ask", ask)
    return Terminal


def test_crash_exits_3_with_traceback(real_archive, output, crash, endpoint, capsys):
    assert main([str(real_archive), "-o", str(output), "--no-report-bugs"]) == EXIT_CRASHED
    err = capsys.readouterr().err
    assert "Traceback" in err and SECRET in err and "this is a bug" in err
    assert not output.exists()
    assert endpoint == []


def test_flag_sends_without_asking(real_archive, output, crash, endpoint, terminal, capsys):
    assert main([str(real_archive), "-o", str(output), "--report-bugs"]) == EXIT_CRASHED
    assert not terminal.asked
    assert len(endpoint) == 1
    assert f"bug report sent, id {endpoint[0]['report_id']}" in capsys.readouterr().err


def test_env_sends_without_asking(real_archive, crash, endpoint, terminal, monkeypatch):
    monkeypatch.setenv(bugreport.OPT_IN_ENV, "1")
    assert main([str(real_archive), "--dry-run"]) == EXIT_CRASHED
    assert not terminal.asked
    assert len(endpoint) == 1


def test_env_0_neither_sends_nor_asks(real_archive, crash, endpoint, terminal, monkeypatch):
    monkeypatch.setenv(bugreport.OPT_IN_ENV, "0")
    assert main([str(real_archive), "--dry-run"]) == EXIT_CRASHED
    assert not terminal.asked
    assert endpoint == []


def test_flag_overrides_env(real_archive, crash, endpoint, monkeypatch):
    monkeypatch.setenv(bugreport.OPT_IN_ENV, "1")
    assert main([str(real_archive), "--dry-run", "--no-report-bugs"]) == EXIT_CRASHED
    assert endpoint == []


@pytest.mark.parametrize(("answer", "sent"), [("y\n", 1), ("yes\n", 1), ("\n", 0), ("n\n", 0), ("", 0)])
def test_asks_in_a_terminal(real_archive, crash, endpoint, terminal, capsys, answer, sent):
    terminal.answer = answer
    assert main([str(real_archive), "--dry-run"]) == EXIT_CRASHED
    assert terminal.asked
    assert len(endpoint) == sent
    err = capsys.readouterr().err
    assert '"report_id"' in err  # the exact payload is shown before asking


def test_never_asks_or_sends_when_not_a_terminal(real_archive, crash, endpoint, capsys):
    assert main([str(real_archive), "--dry-run"]) == EXIT_CRASHED
    assert endpoint == []
    assert "--report-bugs" in capsys.readouterr().err


def test_refusals_are_not_reported(tmp_path, endpoint):
    assert main([str(tmp_path / "nope.db"), "--dry-run", "--report-bugs"]) == 2
    assert endpoint == []


def test_failed_verification_is_not_reported(real_archive, endpoint, monkeypatch):
    monkeypatch.setattr(migrate_module, "_verify", lambda *args: ["boom"])
    assert main([str(real_archive), "--dry-run", "--report-bugs"]) == 1
    assert endpoint == []


def test_unreachable_endpoint_still_exits_3(real_archive, crash, monkeypatch, capsys):
    monkeypatch.setenv(bugreport.URL_ENV, "http://127.0.0.1:9/")
    assert main([str(real_archive), "--dry-run", "--report-bugs"]) == EXIT_CRASHED
    assert "could not send the bug report" in capsys.readouterr().err


def test_payload_is_anonymous(real_archive, output, crash, endpoint):
    main([str(real_archive), "-o", str(output), "--report-bugs"])
    (report,) = endpoint
    body = json.dumps(report)
    assert SECRET not in body
    assert str(real_archive.parent) not in body and "/" not in body
    assert report["mode"] == "output"
    assert report["exception"]["type"] == "RuntimeError"
    assert report["exception"]["causes"] == ["KeyError"]
    frames = report["exception"]["frames"]
    assert frames[0] == {"module": "apigrunn_archiver_db_migrator.cli", "function": "main",
                         "line": frames[0]["line"]}
    assert frames[-1]["module"] == __name__ and frames[-1]["function"] == "verify"
    assert set(report) == {
        "payload_version", "report_id", "tool", "tool_version", "archiver_version",
        "python", "os", "machine", "mode", "drop_error_bodies", "exception",
    }
