"""Multi-agent trading floor (Phase 7).

Each :class:`~botcore.agents.base.Agent` is a named signal source with its own
tracked shadow P&L. A :class:`~botcore.agents.coordinator.Coordinator` blends the
enabled agents into one book. An agent whose shadow P&L falls past its floor is
permanently disabled (``data/agents/<id>.DEAD``) and dropped from the coordinator
— the bot keeps trading with the survivors.
"""

from botcore.agents.base import Agent, AgentContext, AgentSignal

__all__ = ["Agent", "AgentContext", "AgentSignal"]
