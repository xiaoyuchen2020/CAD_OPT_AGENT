"""Freeze a relevant capability catalog; append newly required tools, never churn."""
from cad_agent.analysis.tool_registry import tool_definitions
from cad_agent.analysis.tool_metadata import GROUPS, CONTROL


class StableTools:
    def __init__(self, session):
        self.catalog = {t["function"]["name"]: t for t in tool_definitions(session.policy.enabled, session)}
        families = session.focus.families() if session.focused_enabled else set(GROUPS)
        names = set(CONTROL) | set().union(*(GROUPS[f] for f in families))
        from cad_agent.analysis.capabilities import profile
        if session.focused_enabled and profile(session) == "assembly_lookup":
            names = session.focus.names() | {"finish_analysis", "read_evidence_record"}
        self.definitions = [self.catalog[n] for n in sorted(names & self.catalog.keys())]

    def include_required(self, allowed):
        known = {t["function"]["name"] for t in self.definitions}
        added = sorted(set(allowed) & self.catalog.keys() - known)
        self.definitions = [*self.definitions, *(self.catalog[n] for n in added)]
        return added
