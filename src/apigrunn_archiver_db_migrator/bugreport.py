"""Opt-in bug reports for crashes of the command-line interface.

Only exceptions that are bugs are reported: refusals (:class:`MigratorError`),
failed verification and file system errors are handled by the CLI and never
get here. Nothing is sent unless the user opts in, with ``--report-bugs`` or
``APIGRUNN_MIGRATOR_REPORT_BUGS=1``, or by answering yes when asked. They are
asked only when both stdin and stderr are a terminal.

A report is anonymous: it holds versions, the platform, the mode the tool ran
in, the exception's type, and the module, function and line of each frame of
its traceback. It never holds the exception's message, file paths, URLs or
anything else read from an archive, since those can identify the user.

The library never reports anything; only :func:`cli.main` calls this module.
"""

from __future__ import annotations

import json
import os
import platform
import sys
import traceback
import urllib.request
import uuid
from typing import Any

from apigrunn_archiver import __version__ as archiver_version

from ._version import __version__

REPORT_URL = "https://REPLACE-ME.example/apigrunn-archiver-db-migrator/crash"

#: Set to ``1`` to send reports without asking, ``0`` to never send or ask.
OPT_IN_ENV = "APIGRUNN_MIGRATOR_REPORT_BUGS"
#: Overrides :data:`REPORT_URL`, for testing.
URL_ENV = "APIGRUNN_MIGRATOR_REPORT_URL"

PAYLOAD_VERSION = 1
MAX_FRAMES = 50
TIMEOUT = 5


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
        "payload_version": PAYLOAD_VERSION,
        "report_id": str(uuid.uuid4()),
        "tool": "apigrunn-archiver-db-migrator",
        "tool_version": __version__,
        "archiver_version": archiver_version,
        "python": f"{platform.python_implementation()} {platform.python_version()}",
        "os": platform.system(),
        "machine": platform.machine(),
        "mode": mode,
        "drop_error_bodies": drop_error_bodies,
        "exception": {
            "type": _type_name(exc),
            "causes": [_type_name(c) for c in _chain(exc)],
            # The innermost frames are where it went wrong; keep those.
            "frames": frames[-MAX_FRAMES:],
        },
    }


def send_report(report: dict[str, Any], url: str | None = None) -> bool:
    """POST ``report`` as JSON; whether it was accepted. Never raises."""
    url = url or os.environ.get(URL_ENV) or REPORT_URL
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
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return 200 <= response.status < 300
    except Exception:  # noqa: BLE001 - a failed report must not hide the crash
        return False


def handle_crash(
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
    if opt_in is None and not (_interactive() and _ask(report)):
        print(f"you can report this bug by running again with --report-bugs, "
              f"or set {OPT_IN_ENV}=1", file=sys.stderr)
        return

    if send_report(report):
        print(f"bug report sent, id {report['report_id']}; thank you", file=sys.stderr)
    else:
        print("could not send the bug report", file=sys.stderr)


def _env_opt_in() -> bool | None:
    value = os.environ.get(OPT_IN_ENV, "").strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return None


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stderr.isatty()


def _ask(report: dict[str, Any]) -> bool:
    print("\nThis is a bug in apigrunn-archiver-db-migrator. This anonymous report would be "
          "sent to its developers:\n", file=sys.stderr)
    print(json.dumps(report, indent=2), file=sys.stderr)
    print(f"\nSend it? [y/N] (--report-bugs or {OPT_IN_ENV}=1|0 skips this question) ",
          end="", file=sys.stderr, flush=True)
    try:
        answer = sys.stdin.readline()
    except (KeyboardInterrupt, EOFError):
        print(file=sys.stderr)
        return False
    return answer.strip().lower() in ("y", "yes")


def _type_name(exc: BaseException) -> str:
    cls = type(exc)
    return cls.__qualname__ if cls.__module__ == "builtins" else f"{cls.__module__}.{cls.__qualname__}"


def _chain(exc: BaseException) -> list[BaseException]:
    """The exceptions ``exc`` was raised from or while handling, nearest first."""
    chain: list[BaseException] = []
    current = exc.__cause__ or exc.__context__
    while current is not None and current not in chain and len(chain) < 10:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain
