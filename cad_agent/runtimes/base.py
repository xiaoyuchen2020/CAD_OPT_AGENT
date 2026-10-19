"""Harness-neutral contract. Runtimes never own CAD evidence or execute code."""

from typing import Protocol

from cad_agent.analysis.session import CadAgentSession


class AgentRuntime(Protocol):
    name: str

    def run(self, session: CadAgentSession, history: list | None = None) -> dict:
        """Use session.invoke_tool and session.finish; return a verified answer."""
        ...
