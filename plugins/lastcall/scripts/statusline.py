#!/usr/bin/env python3
"""Optional status line that teaches Last Call the exact context window.

The Stop hook cannot see how big the window is. The transcript records the
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
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from lastcall import (band_for, load_config, read_state, state_dir,
                               write_state)
except ImportError:  # standalone copy — degrade to printing only
    band_for = load_config = read_state = state_dir = write_state = None

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


def main(argv):
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

    # Cache the window for the Stop hook. This is the whole point of the file.
    if window and session_id and load_config:
        try:
            config = load_config(payload)
            state = read_state(config, session_id)
            if state.get("window_from_statusline") != window:
                state["window_from_statusline"] = window
                write_state(config, session_id, state)
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
                band = band_for(percent, load_config(payload))
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
