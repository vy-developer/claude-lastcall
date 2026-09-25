"""Agent adapter registry.

    from lastcall_core.agents import detect_agent
    agent = detect_agent(payload)            # hook stdin, os.environ
    usage = agent.read_usage(payload.get("transcript_path"),
                             payload.get("session_id"),
                             model=payload.get("model"))
    output = agent.format_output(event, message, block_reason)
"""

import os

from .base import (EVIDENCE_ENV, EVIDENCE_NONE, EVIDENCE_PATH, EVIDENCE_PAYLOAD,
                   Agent, Usage)
from .claude import ClaudeAgent
from .codex import CodexAgent

# Registration order breaks ties, and Claude comes first: it is what every
# existing install runs, so an invocation with no evidence either way keeps
# behaving exactly as before.
AGENTS = (ClaudeAgent(), CodexAgent())

#: Environment variable that forces the choice ("claude" or "codex").
OVERRIDE_ENV = "LASTCALL_AGENT"

__all__ = ["AGENTS", "Agent", "ClaudeAgent", "CodexAgent", "Usage",
           "detect_agent", "get_agent", "rank_agents", "OVERRIDE_ENV",
           "EVIDENCE_NONE", "EVIDENCE_ENV", "EVIDENCE_PAYLOAD", "EVIDENCE_PATH"]


def get_agent(name):
    """The adapter called ``name``, or None."""
    name = (name or "").strip().lower()
    for agent in AGENTS:
        if agent.name == name:
            return agent
    return None


def rank_agents(payload=None, env=None):
    """[(evidence, agent)] strongest first, ties in registration order."""
    payload = payload if isinstance(payload, dict) else {}
    env = os.environ if env is None else env
    scored = [(agent.evidence(payload, env), index, agent)
              for index, agent in enumerate(AGENTS)]
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [(score, agent) for score, _index, agent in scored]


def detect_agent(payload=None, env=None, default="claude"):
    """Which agent fired this hook.

    Strongest evidence wins: $LASTCALL_AGENT, then where the transcript
    lives, then fields only one agent sends (Codex's turn_id), then
    environment markers. With no evidence at all, ``default``.
    """
    env = os.environ if env is None else env
    forced = get_agent(env.get(OVERRIDE_ENV))
    if forced is not None:
        return forced
    ranked = rank_agents(payload, env)
    if ranked and ranked[0][0] > EVIDENCE_NONE:
        return ranked[0][1]
    return get_agent(default) or AGENTS[0]
