"""Typed contracts assembled from the existing authoritative CAD registries.

All model tools own session effects, including apparent reads. Only explicitly
registered detached computations may execute concurrently.
"""
from dataclasses import dataclass
import json

VERSION = 'cad-tool-contract-v1'


class InvalidToolOutput(ValueError):
    pass


@dataclass(frozen=True)
class ToolContract:
    name: str
    input_model: type
    description: str
    families: tuple[str, ...]
    effects: tuple[str, ...]
    prerequisites: tuple[str, ...]
    parallel_safe: bool = False
    version: str = VERSION

    def validate_output(self, result):
        # Domain validators remain authoritative for units, provenance and meaning.
        if not isinstance(result, dict):
            raise InvalidToolOutput('TOOL_OUTPUT_INVALID: expected a JSON object')
        try:
            return json.loads(json.dumps(result, ensure_ascii=False, allow_nan=False))
        except (TypeError, ValueError, OverflowError) as error:
            raise InvalidToolOutput('TOOL_OUTPUT_INVALID: non-finite or non-JSON result') from error

    def manifest(self):
        return dict(contract_version=self.version, effects=list(self.effects),
                    prerequisites=list(self.prerequisites), parallel_safe=self.parallel_safe,
                    output_schema={'type': 'object'}, result_projection='domain_source_preserving',
                    result_semantics='tool_success_does_not_grant_evidence_or_completion',
                    retry_policy='reconcile_receipt_before_retry',
                    cancellation='cooperative_with_late_publication_fence')


def contract_for(name):
    # Lazy imports keep schema ownership out of the execution kernel.
    from cad_agent.analysis.tool_registry import TOOL_SPECS, VISUAL_TOOLS
    from cad_agent.analysis.tool_metadata import GROUPS, CONTROL, READS
    if name not in TOOL_SPECS:
        raise ValueError('未知工具')
    schema, description = TOOL_SPECS[name]
    effects = ('task_state',) if name in CONTROL else ('task_state', 'observation_registration')
    if name in READS:
        effects += ('source_read_coverage',)
    prerequisites = ('selected_snapshot', 'authorized_capability', 'current_requirement_scope', 'remaining_budget')
    if name in VISUAL_TOOLS:
        prerequisites += ('visual_provider',)
    return ToolContract(name, schema, description,
        tuple(sorted(group for group, names in GROUPS.items() if name in names)), effects, prerequisites)
