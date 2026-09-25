"""UTF-8 on the standard streams, whatever the platform's default.

Windows gives a redirected stream the ANSI code page (cp1252 on most
machines). That code page cannot encode the check marks and arrows the CLI
prints, so print() raised UnicodeEncodeError, and what it could encode (an em
dash) reached a UTF-8 reader as an undecodable byte. It also mis-decodes the
UTF-8 JSON an agent pipes to a hook. The agents, their desktop apps and the
terminals on every platform speak UTF-8, so every entry point speaks it too,
and degrades a character it cannot write rather than the command.
"""

import sys


def utf8_stdio():
    """Reconfigure stdout and stderr, and stdin when it is not a terminal,
    to UTF-8 with errors="replace". A stream without ``reconfigure`` (a test's
    StringIO, a closed or detached stream) is left alone. Never raises."""
    for stream in (sys.stdout, sys.stderr):
        _reconfigure(stream)
    stdin = sys.stdin
    try:
        piped = stdin is not None and not stdin.isatty()
    except (AttributeError, ValueError, OSError):
        piped = False
    if piped:
        _reconfigure(stdin)


def _reconfigure(stream):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - AttributeError, ValueError, OSError, io.UnsupportedOperation
        pass
