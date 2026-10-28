"""Freeze a relevant capability catalog; append newly required tools, never churn."""
from copy import deepcopy
import hashlib
import json
from cad_agent.analysis.tool_registry import tool_definitions
from cad_agent.analysis.tool_metadata import GROUPS, CONTROL


class StableTools:
    def __init__(self, session):
        self.catalog = {t["function"]["name"]: deepcopy(t) for t in tool_definitions(session.policy.enabled, session)}
        self.generation = 0
        families = session.focus.families() if session.focused_enabled else set(GROUPS)
        names = set(CONTROL) | set().union(*(GROUPS[f] for f in families))
        from cad_agent.analysis.capabilities import profile
        if session.focused_enabled and profile(session) == "assembly_lookup":
            names = session.focus.names() | {"finish_analysis", "read_evidence_record"}
        self._definitions = [self.catalog[n] for n in sorted(names & self.catalog.keys())]

    @property
    def definitions(self):
        # Provider shaping must never mutate the admitted shared schema.
        return deepcopy(self._definitions)

    def snapshot(self):
        serialized = json.dumps(self._definitions, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
        return dict(generation=self.generation,
                    sha256=hashlib.sha256(serialized.encode()).hexdigest(),
                    names=[item['function']['name'] for item in self._definitions])

    def include_required(self, allowed):
        known = {t["function"]["name"] for t in self._definitions}
        added = sorted(set(allowed) & self.catalog.keys() - known)
        self._definitions = [*self._definitions, *(self.catalog[n] for n in added)]
        if added:
            self.generation += 1
        return added
