#!/usr/bin/env python3
"""Optional status line that teaches Last Call the exact context window.

The hooks cannot see how big a Claude Code session's window is. The transcript records the
model as e.g. "claude-opus-5" whether that session has a 200K window or a 1M
one, so the size is genuinely not derivable there — measured, not assumed.

Claude Code's status line, however, is handed the real number. This script
prints a normal status line AND caches that number where the guard can find it.
Install it and the guard stops needing to be told anything.

    "statusLine": {
      "type": "command",
      "command": "python3 /path/to/statusline.py"
    }

Run `statusline.py --dump` to print the raw payload your Claude Code version
sends, which is the way to check the field names below against reality.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from lastcall import band_for, load_config, read_state, update_state
except ImportError:  # standalone copy — degrade to printing only
    band_for = load_config = read_state = update_state = None
try:
    from lastcall_core.windows import record_learned
except ImportError:
    record_learned = None

# Field names vary across Claude Code versions, so match on shape rather than
# betting the feature on one spelling. --dump exists for when none of these hit.
_WINDOW_KEYS = ("context_window_size", "context_window_tokens", "window_size",
                "max_context_tokens", "context_window")
_USED_KEYS = ("context_used_tokens", "used_tokens", "context_tokens",
              "total_tokens", "input_tokens")


def deep_find(payload, keys):
    """Search nested dicts for the first matching key with a usable number."""
    stack = [payload]
    while stack:
        node = stack.pop(0)
        if not isinstance(node, dict):
            continue
        for key in keys:
            value = node.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)) and value > 0:
                return int(value)
            if isinstance(value, str) and value.strip().isdigit():
                return int(value.strip())
        for value in node.values():
            if isinstance(value, dict):
                stack.append(value)
    return None


def gauge(percent, width=10):
    filled = max(0, min(width, int(round(percent / 100.0 * width))))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def utf8_stdio():
    """Claude Code pipes UTF-8 JSON in and reads UTF-8 back; a redirected
    stream on Windows would be cp1252. Self-contained, for a standalone copy."""
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def main(argv):
    utf8_stdio()
    raw = sys.stdin.read() if not sys.stdin.isatty() else "{}"
    try:
        payload = json.loads(raw or "{}")
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    if "--dump" in argv:
        print(json.dumps(payload, indent=2))
        return 0

    window = deep_find(payload, _WINDOW_KEYS)
    used = deep_find(payload, _USED_KEYS)
    session_id = payload.get("session_id") or (payload.get("session") or {}).get("id")

    # Cache the window for the hooks. This is the whole point of the file.
    # update_state touches only this one field, re-reading the file first, so
    # a hook writing the same session's state at the same moment keeps its
    # fields and this one keeps its own. The status line is Claude Code's.
    if window and session_id and load_config:
        try:
            config = load_config(payload)
            state = read_state(config, session_id, "claude")
            if state.get("window_from_statusline") != window:
                update_state(config, session_id,
                             {"window_from_statusline": window}, "claude")
                # And remember it for the model, so the next session on it
                # starts with the right window before this script first runs.
                # Transcripts record the model without "[1m]", so neither
                # does the learned entry.
                model_id = (payload.get("model") or {}).get("id") \
                    if isinstance(payload.get("model"), dict) else None
                if record_learned and isinstance(model_id, str) and model_id.strip():
                    base = model_id.replace("[1m]", "").replace("[1M]", "").strip()
                    record_learned(config, "claude", base, window, "statusline")
        except Exception:  # noqa: BLE001 - a status line must never fail loudly
            pass

    model = (payload.get("model") or {})
    model_name = model.get("display_name") or model.get("id") or ""
    directory = payload.get("workspace", {}).get("current_dir") or payload.get("cwd") or ""
    parts = []
    if model_name:
        parts.append(model_name)
    if directory:
        parts.append(os.path.basename(directory.rstrip("/")) or directory)

    if window and used:
        percent = used * 100.0 / window
        label = "ctx %s %.0f%%" % (gauge(percent), percent)
        if band_for and load_config:
            try:
                # Pass the real window. Without it band_for rebuilds the
                # token count against the CONFIGURED window (or 1M), so zones
                # written in tokens showed RED on a 200k session at 75%.
                band = band_for(percent, load_config(payload), window)
                if band != "green":
                    label += " " + band.upper()
            except Exception:  # noqa: BLE001
                pass
        parts.append(label)
    elif window:
        parts.append("ctx window %s" % "{:,}".format(window))

    print(" | ".join(parts) if parts else "lastcall")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:  # noqa: BLE001
        print("")
        sys.exit(0)
