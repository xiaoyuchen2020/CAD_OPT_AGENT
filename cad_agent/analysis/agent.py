"""Compatibility entry point; CAD tools and verification live outside runtimes."""

from cad_agent.analysis.session import (
    CadAgentSession,
    AGENT_VERSION,
    SYSTEM,
    TOOL_SPECS,
    FinalAnswer,
    source_context,
    tool_definitions,
)


class AgentRunner(CadAgentSession):
    def run(self, history=None):
        from cad_agent.runtimes.registry import run_agent

        return run_agent(self, history)
