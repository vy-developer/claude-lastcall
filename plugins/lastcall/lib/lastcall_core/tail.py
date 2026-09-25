"""Bounded reverse reading of append-only JSONL logs.

Both agents write their session history as JSONL that only ever grows, and the
record a hook needs is always one of the newest. Reading forward means parsing
the whole history on every Stop, and that cost grows without bound over a long
session, which is exactly when the guard matters most. So files are read from
the end, in chunks, up to a byte limit.
"""

import os

# How far back to read before giving up. The newest usage record is almost
# always within a few KB of the end; the bound exists so a multi-hundred-MB log
# cannot stall the hook. Codex compaction records alone can run to several MB,
# which is why callers that must look past one pass a larger limit.
TAIL_LIMIT_BYTES = 8 * 1024 * 1024
TAIL_CHUNK_BYTES = 256 * 1024


def iter_lines_reverse(path, limit=TAIL_LIMIT_BYTES, chunk=TAIL_CHUNK_BYTES):
    """Yield non-empty lines (bytes, without the newline) from the end of a
    file backwards, reading at most ``limit`` bytes.

    A line that starts before the limit is never yielded half-read: the
    fragment is dropped, not parsed as if it were whole. Fragments of a long
    line are kept as a list and joined once, so a multi-MB line costs one copy
    rather than one per chunk.
    """
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        consumed = 0
        # Pieces of the line currently being assembled, newest piece first.
        # That line extends to the right of the block being scanned.
        pieces = []
        while position > 0 and consumed < limit:
            step = min(chunk, position)
            position -= step
            handle.seek(position)
            block = handle.read(step)
            consumed += step
            end = len(block)
            while True:
                newline = block.rfind(b"\n", 0, end)
                if newline == -1:
                    pieces.append(block[:end])
                    break
                piece = block[newline + 1:end]
                if pieces:
                    pieces.append(piece)
                    line = b"".join(reversed(pieces))
                    pieces = []
                else:
                    line = piece
                end = newline
                line = _clean(line)
                if line is not None:
                    yield line
        if position == 0 and pieces:
            line = _clean(b"".join(reversed(pieces)))
            if line is not None:
                yield line


def _clean(line):
    # rstrip the CR: a log written on Windows is CRLF, and splitting on LF
    # alone leaves it dangling on every line.
    line = line.rstrip(b"\r")
    return line if line.strip() else None
