"""Zones (the thresholds) and the window they are measured against."""

from .agents.claude import (EXTENDED_WINDOW, KNOWN_WINDOWS, STANDARD_WINDOW,
                            window_from_evidence)

__all__ = ["DEFAULT_HEADLINES", "EXTENDED_WINDOW", "KNOWN_WINDOWS",
           "STANDARD_WINDOW", "band_for", "describe_threshold",
           "resolve_window", "resolve_zones", "window_from_evidence",
           "zone_for", "zone_threshold"]

DEFAULT_HEADLINES = {
    "yellow": ("Finish what is in flight; start nothing new. This is an alarm, "
               "not a decision — you judge what still fits."),
    "red": ("Closing time. Wrap-up only — do not start, resume, or 'quickly "
            "finish' anything."),
}

# Window sources that are read from the agent itself rather than deduced.
EXACT_USAGE_SOURCES = ("transcript", "models-cache", "model-name")


def _fmt(number):
    return "{:,}".format(number)


def resolve_window(config, state=None, usage=None, agent=None):
    """(window, source) from exact sources first, proof second, else
    (None, "unknown"). Never a guess: the fallback lives in the engine.

    Codex writes the usable window into every rollout, so that figure wins
    over everything, config included — a global config written for a 1M
    Claude session would otherwise make every Codex session read as 20% full.

    Claude Code does not, so its order is the one this tool has always used:
    config (unless the tokens in use disprove it), the status-line cache, an
    explicit [1m] model marker, then proof from the tokens observed.
    """
    state = state or {}
    agent = agent or (getattr(usage, "agent", None)) or "claude"
    observed = int(state.get("max_observed") or 0)
    configured = config.get("context_window_tokens")
    exact = None
    if usage is not None and usage.window and usage.window_source in EXACT_USAGE_SOURCES:
        exact = (int(usage.window), usage.window_source)

    if agent == "codex" and exact:
        return exact

    if configured:
        configured = int(configured)
        # A session cannot hold more tokens than its window, so an observation
        # above the configured figure DISPROVES it. Trusting the config anyway
        # produced 372% and a permanent RED — the guard shouting on an empty
        # session. Correct it and say so.
        if observed > configured and agent != "codex":
            proven = window_from_evidence(observed)
            if proven:
                return proven, ("config says %s but %s tokens are in use — "
                                "using %s" % (_fmt(configured), _fmt(observed),
                                              _fmt(proven)))
        return configured, "config"

    if agent != "codex":
        # Written by the optional status-line helper, which receives the real
        # context_window_size from Claude Code.
        from_statusline = state.get("window_from_statusline")
        if from_statusline:
            return int(from_statusline), "statusline"

    if exact:
        return exact

    if agent != "codex":
        # max_observed is monotonic for the life of the session. It must NOT be
        # the counter compaction resets: compaction changes how full the
        # window is, never how big it is.
        proven = window_from_evidence(observed)
        if proven:
            return proven, "proven by %s tokens observed" % _fmt(observed)

    return None, "unknown"


def resolve_zones(config):
    """The thresholds, lowest first.

    Two zones named yellow and red are just the default arrangement, not a
    built-in limit.
    """
    declared = config.get("zones")
    if not declared:
        declared = [
            {"name": "yellow", "at": config.get("yellow_percent")},
            {"name": "red", "at": config.get("red_percent"), "block": True},
        ]
    if not isinstance(declared, (list, tuple)):
        return []

    zones = []
    for entry in declared:
        if not isinstance(entry, dict):
            continue
        # A zone fires either at a PERCENTAGE of the window ("at") or at an
        # absolute token count ("at_tokens"). Absolute zones need no window.
        at = None
        at_tokens = None
        if entry.get("at_tokens") is not None:
            try:
                at_tokens = int(entry["at_tokens"])
            except (TypeError, ValueError):
                continue
        else:
            try:
                at = float(entry.get("at"))
            except (TypeError, ValueError):
                continue  # a malformed zone is dropped, not fatal
        name = str(entry.get("name")
                   or ("%s tokens" % _fmt(at_tokens)
                       if at_tokens is not None else "%g%%" % at))
        zones.append({
            "name": name,
            "at": at,
            "at_tokens": at_tokens,
            "message": entry.get("message"),
            "template": entry.get("template"),
            "headline": entry.get("headline") or DEFAULT_HEADLINES.get(name),
            "block": bool(entry.get("block", False)),
        })
    zones.sort(key=lambda zone: (zone["at_tokens"] is None,
                                 zone["at_tokens"] if zone["at_tokens"] is not None
                                 else zone["at"]))
    return zones


def zone_threshold(zone, window):
    """The token count at which ``zone`` fires, or None when it is a
    percentage and the window is unknown."""
    if zone["at_tokens"] is not None:
        return zone["at_tokens"]
    if window:
        return window * zone["at"] / 100.0
    return None


def zone_for(tokens, window, zones):
    """The highest zone this reading has reached, or None below them all.

    A percentage zone is skipped when the window is unknown; an absolute zone
    never needs one.
    """
    reached = []
    for zone in zones:
        threshold = zone_threshold(zone, window)
        if threshold is not None and tokens >= threshold:
            reached.append((threshold, zone))
    if not reached:
        return None
    reached.sort(key=lambda pair: pair[0])
    return reached[-1][1]


def describe_threshold(zone):
    """Where a zone fires, the way a person would say it: "40%" or "400k"."""
    tokens = zone.get("at_tokens")
    if tokens is None:
        return "%g%%" % zone["at"]
    if tokens >= 1_000_000 and tokens % 100_000 == 0:
        return "%gM" % (tokens / 1_000_000.0)
    if tokens >= 1_000 and tokens % 1_000 == 0:
        return "%dk" % (tokens // 1_000)
    return _fmt(tokens)


def band_for(percent, config, window=None):
    """Zone name for a reading, or "green" below every zone.

    Takes a percentage for backwards compatibility; absolute zones need real
    token counts, so pass the window when you have one.
    """
    window = window or config.get("context_window_tokens") or 1_000_000
    tokens = int(window * percent / 100.0)
    zone = zone_for(tokens, window, resolve_zones(config))
    return zone["name"] if zone else "green"
