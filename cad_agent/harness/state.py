"""Immutable, JSON-serializable admission snapshots for a single model step."""
from dataclasses import dataclass, asdict
from hashlib import sha256
import json


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), default=str)


@dataclass(frozen=True)
class StepContext:
    step_id: str
    ordinal: int
    protocol: str
    final_only: bool
    input_revision: int
    source_hashes: tuple[tuple[str, str], ...]
    question: str
    requirements_json: str
    tool_catalog_json: str
    admission_tool_names: tuple[str, ...]
    tool_admission_policy: str
    authorization_json: str
    policy_version: str
    model: str
    reasoning_effort: str | None
    remaining_tool_calls: int

    @classmethod
    def capture(cls, session, ordinal, protocol, tools, final_only, admission_names=None):
        from cad_agent.analysis.method_authorization import authorization
        task = session.task.data
        revision = int(task.get('input_revision', getattr(session, 'input_revision', 0)))
        sources = tuple((sid, session.w.store.get('drawing', sid)['sha256'])
                        for sid in sorted(session.allowed_snapshots))
        run_id = getattr(session.journal, 'run_id', None) or task['task_id']
        return cls(f'{run_id}:step:{ordinal}', ordinal, protocol, final_only, revision,
                   sources, session.request.question,
                   canonical([{'id': s['id'], 'requirement': s['requirement'],
                               'operations': s.get('operations', []),
                               'coverage': s.get('coverage', [])} for s in task['subtasks']]),
                   canonical(tools), tuple(sorted(admission_names if admission_names is not None else
                       (tool.get('function', tool).get('name', '') for tool in tools))),
                   'native_capability_catalog' if admission_names is not None else 'advertised_tools',
                   canonical(authorization(session.request)),
                   task.get('execution_policy_version', 'legacy'), session.w.settings.model,
                   getattr(session.w.settings, 'reasoning_effort', None),
                   max(0, session.w.settings.agent_max_tool_calls - session.calls))

    def public(self):
        result = asdict(self)
        result['sha256'] = sha256(canonical(result).encode()).hexdigest()
        return result


@dataclass(frozen=True)
class StepDecision:
    final_only: bool
    correction_only: bool
    stop_reason: str | None


@dataclass(frozen=True)
class LoopPolicy:
    """Differences are explicit policy; transport never determines CAD validity."""
    reserve_rounds: int = 1
    repair_requires_final: bool = True
    require_successful_tool_for_final: bool = False

    def decide(self, session, ordinal, *, reason=None, repairing=False):
        final = bool(reason or ordinal >= session.round_limit - self.reserve_rounds
                     or (self.repair_requires_final and repairing))
        return StepDecision(final, ordinal == session.round_limit,
                            reason or ('answer_repair' if repairing else 'exploration_budget') if final else None)
