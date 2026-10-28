"""Protocol conversion only; CAD dispatch and permission decisions live in Driver."""
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: Any
    namespace: str | None = None


class ChatAdapter:
    protocol = 'chat'
    error_prefix = ''
    strict_global_call_ids = False  # IDs are scoped to the model step.

    @staticmethod
    def calls(message):
        return [ToolCall(c.get('id', ''), c.get('function', {}).get('name', ''),
                         c.get('function', {}).get('arguments', '{}'))
                for c in message.get('tool_calls', [])]

    @staticmethod
    def result(call, output):
        return {'role': 'tool', 'tool_call_id': call.call_id, 'content': output}


class ResponsesAdapter:
    protocol = 'responses'
    error_prefix = 'SIWC_'
    strict_global_call_ids = True

    @staticmethod
    def calls(output):
        return [ToolCall(c.get('call_id', ''), c.get('name', ''), c.get('arguments', '{}'),
                         c.get('namespace')) for c in output if c.get('type') == 'function_call']

    @staticmethod
    def result(call, output):
        return {'type': 'function_call_output', 'call_id': call.call_id, 'output': output}
