"""Zones (the thresholds) and the window they are measured against."""

from .agents.claude import (EXTENDED_WINDOW, KNOWN_WINDOWS, STANDARD_WINDOW,
                            window_from_evidence)

__all__ = ["DEFAULT_HEADLINES", "EXTENDED_WINDOW", "KNOWN_WINDOWS",
           "STANDARD_WINDOW", "band_for", "describe_threshold",
           "effective_window", "resolve_window", "resolve_zones",
           "session_window", "window_from_evidence", "zone_for",
           "zone_threshold"]

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


def _learned(learned):
    """``learned`` may be a dict or a zero-argument callable returning one,
    so the file is only read when resolution actually gets that far."""
    if callable(learned):
        try:
            learned = learned()
        except Exception:  # noqa: BLE001 - a hint must never break the hook
            learned = None
    return learned if isinstance(learned, dict) else {}


def _checked(window, label, observed, agent):
    """(window, source), corrected when the tokens in use disprove it.

    A session cannot hold more tokens than its window, so an observation
    above any claimed figure DISPROVES the claim. Trusting a configured window
    anyway produced 372% and a permanent RED — the guard shouting on an empty
    session. The same holds for the map, a learned window, a stale status
    line: correct it and say so.
    """
    if observed > window and agent != "codex":
        proven = window_from_evidence(observed)
        if proven:
            return proven, ("%s says %s but %s tokens are in use — using %s"
                            % (label, _fmt(window), _fmt(observed), _fmt(proven)))
    return window, label


def resolve_window(config, state=None, usage=None, agent=None, learned=None,
                   settings=None):
    """(window, source) from exact sources first, proof second, else
    (None, "unknown"). Never a guess: the fallback lives in effective_window.

    Codex writes the usable window into every rollout, so that figure wins
    over everything, config included — a global config written for a 1M
    Claude session would otherwise make every Codex session read as 20% full.

    Claude Code does not, so its order is:

      1. context_window_tokens              "config"
      2. the status-line cache              "statusline"
      3. the `windows` map for this model   "map"
      4. the window learned for this model  "learned"
      5. an explicit [1m] in the model      "model-name"
      6. a [1m] model in settings/env       "configured model ..." (``settings``)
      7. tokens in use above 200K           "proven by N tokens observed"

    Every one of 1-6 is corrected when the tokens in use disprove it.
    ``learned`` is the learned map (or a callable returning it) and
    ``settings`` a callable returning (window, source); both are optional so
    this stays a pure function for callers that have neither.
    """
    from .windows import learned_window, match_window

    state = state or {}
    agent = agent or (getattr(usage, "agent", None)) or "claude"
    observed = int(state.get("max_observed") or 0)
    model = getattr(usage, "model", None)
    exact = None
    if usage is not None and usage.window and usage.window_source in EXACT_USAGE_SOURCES:
        exact = (int(usage.window), usage.window_source)

    if agent == "codex" and exact:
        return exact

    configured = config.get("context_window_tokens")
    if configured:
        return _checked(int(configured), "config", observed, agent)

    if agent != "codex":
        # Written by the optional status-line helper, which receives the real
        # context_window_size from Claude Code.
        from_statusline = state.get("window_from_statusline")
        if from_statusline:
            return _checked(int(from_statusline), "statusline", observed, agent)

    mapped, _key = match_window(config.get("windows"), agent, model)
    if mapped:
        return _checked(mapped, "map", observed, agent)

    remembered, _entry = learned_window(_learned(learned), agent, model)
    if remembered:
        return _checked(remembered, "learned", observed, agent)

    if exact:
        return _checked(exact[0], exact[1], observed, agent)

    if agent != "codex":
        if settings is not None:
            try:
                hinted, source = settings()
            except Exception:  # noqa: BLE001
                hinted, source = None, "unknown"
            if hinted:
                return _checked(int(hinted), source, observed, agent)
        # max_observed is monotonic for the life of the session. It must NOT be
        # the counter compaction resets: compaction changes how full the
        # window is, never how big it is.
        proven = window_from_evidence(observed)
        if proven:
            return proven, "proven by %s tokens observed" % _fmt(observed)

    return None, "unknown"


def effective_window(config, state, usage, agent=None, env=None, learned=None):
    """(window, source, assumed) for judging ``usage``: resolve_window, then
    the fallback window, flagged as assumed — but only while the tokens in
    use fit in it; beyond it, evidence has already proved a bigger window or
    nothing can be said.

    A Claude window LEARNED from another session and bigger than the standard
    one is assumed too, until this session's own tokens prove it: the model id
    is shared by the 200K and the 1M variant, so one 1M session must not
    silence the guard for every 200K session after it.

    ``learned`` defaults to reading <state_dir>/windows.json (lazily).
    """
    from .agents import get_agent
    from .windows import load_learned

    agent = agent or getattr(usage, "agent", None) or "claude"
    adapter = get_agent(agent)
    evidence = dict(state or {})
    evidence["max_observed"] = max(int(evidence.get("max_observed") or 0),
                                   int(usage.tokens or 0))
    if learned is None:
        learned = lambda: load_learned(config)  # noqa: E731
    hinted = getattr(adapter, "settings_window", None)
    settings = (lambda: hinted(usage.model, env)) if hinted else None  # noqa: E731
    window, source = resolve_window(config, evidence, usage, agent,
                                    learned=learned, settings=settings)
    if window:
        unproven = (agent != "codex" and source == "learned" and window > STANDARD_WINDOW
                    and evidence["max_observed"] <= STANDARD_WINDOW)
        return window, source, unproven
    fallback = config.get("fallback_window_tokens")
    if fallback and usage.tokens <= int(fallback):
        return int(fallback), "assumed", True
    return None, "unknown", False


def session_window(usage, config=None, cwd=None, env=None):
    """(window, source, assumed) for any session's Usage, exactly as the hooks
    would judge it — for `lastcall status` and anything else outside a hook.

    Loads the config that applies at ``cwd`` (when ``config`` is not given),
    that session's state (status-line cache, max tokens observed) and the
    learned windows. Codex sessions get the rollout's own figure.
    """
    from .config import load_config
    from .state import read_state

    if usage is None or getattr(usage, "tokens", None) is None:
        return None, "unknown", False
    if config is None:
        config = load_config({"cwd": cwd} if cwd else {}, env)
    agent = getattr(usage, "agent", None) or "claude"
    state = {}
    if getattr(usage, "session_id", None):
        try:
            state = read_state(config, usage.session_id, agent)
        except Exception:  # noqa: BLE001 - status must never fail on state
            state = {}
    return effective_window(config, state, usage, agent, env)


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
