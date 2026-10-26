"""Subscription transport for external harnesses; does not own their tool loop.

The harness only sees a random loopback credential. OAuth stays in the parent.
Canonical CAD functions and completed Responses items share the direct adapter.
"""
import json
import time

from cad_agent.adapters.siwc import fail
from cad_agent.adapters.model import ModelError
from cad_agent.analysis.efficiency import dumps, compact
from cad_agent.analysis.final_context_budget import fit_messages
from cad_agent.runtimes.responses_subscription import ResponsesTransport
from cad_agent.runtimes.subscription import CodexSubscriptionRuntime
from cad_agent.runtimes.vision import attach_images, mark_images_sent


class SiwcBridge:
    def __init__(self, session):
        self.session = session
        self.transport = ResponsesTransport(session.w.siwc_account)
        self.controls = CodexSubscriptionRuntime()
        self.call_items = {}
        self.reasoning_before_call = {}

    def inputs(self, messages):
        instructions, inputs = [], []
        for m in messages:
            role = m['role']
            if role in ('system', 'developer'):
                instructions.append(m.get('content') or '')
                continue
            if role == 'tool':
                call_id = m['tool_call_id']
                if call_id not in self.call_items:
                    raise fail('TOOL_DENIED', 'Harness 返回未知工具调用的结果。')
                value = m.get('content') or ''
                self.controls.remember_context(self.call_items[call_id]['name'], json.loads(value))
                inputs.append({'type': 'function_call_output', 'call_id': call_id, 'output': value})
                continue
            if role not in ('user', 'assistant'):
                raise fail('STREAM_FORMAT', 'Harness 消息角色不受支持。')
            if m.get('content'):
                inputs.append({'role': role, 'content': m['content']})
            for call in m.get('tool_calls') or []:
                item = self.call_items.get(call['id'])
                fn = call['function']
                if not item or item['name'] != fn['name'] or json.loads(item['arguments']) != json.loads(fn['arguments']):
                    raise fail('TOOL_DENIED', 'Harness 工具历史与已完成调用不一致。')
                inputs.extend(self.reasoning_before_call.get(call['id'], []))
                inputs.append(item)
        return '\n\n'.join(instructions), inputs

    def complete(self, messages, tools, final, entry):
        s = self.session
        if s.w.settings.model != 'gpt-6.1-sol' or s.w.settings.provider != 'responses_subscription':
            raise fail('MODEL_MISMATCH', '订阅桥接模型配置不匹配。')
        completed_tool = any(t['status'] == 'completed' for t in s.trace)
        if final and not completed_tool:
            raise fail('EVIDENCE_NOT_OBTAINED', '预算结束但尚无成功图纸工具结果。')
        # Context relation metadata is derived only from tool results already read.
        self.controls.source_contexts = []
        instructions, inputs = self.inputs(messages)
        if final:
            packet = s.final_context(self.controls, s.policy.stop_reason or 'exploration_budget')
            inputs = fit_messages(s, packet, self.controls.FINAL_CONTEXT_CHARS)
        choice = 'none' if final else 'required' if not completed_tool else 'auto'
        definitions = [] if not tools else [{'type': 'namespace', 'name': 'cad',
            'description': '当前图纸的只读 CAD 检索、上下文与证据工具。',
            'tools': [{'type': 'function', 'name': t['function']['name'],
                'description': t['function']['description'], 'parameters': t['function']['parameters'],
                'strict': False} for t in tools]}]
        if not final and not definitions:
            raise fail('TOOL_DENIED', 'Harness 没有提供可用的 CAD 工具。')
        payload = {'model': s.w.settings.model, 'instructions': instructions, 'input': inputs,
            'tools': definitions, 'tool_choice': choice, 'parallel_tool_calls': False,
            'store': False, 'stream': True, 'reasoning': {'effort': 'low'},
            'include': ['reasoning.encrypted_content']}
        if len(dumps(payload)) > 100000:
            raise fail('CONTEXT_BUDGET', '单轮上下文超过本地上限。')
        entry.update(status='started', round=len(s.runtime_diagnostics['model_requests']),
            tool_choice=choice, evidence_count=len(s.evidence), request_chars=len(dumps(payload)),
            answer_repair=bool(s.validation_issue), evidence_coverage=s.coverage.summary())
        tick = time.monotonic()
        payload = attach_images(s, payload, entry)
        try:
            with s.metrics.measure("model_wait"):
                result = s.journal.model_call(payload, lambda: self.transport.request(s, payload),
                    capture_level='wire_payload', title='桥接模型请求', image_inputs=entry.get('image_inputs'))
            mark_images_sent(s, entry)
        except ModelError as error:
            entry.update(status='failed', error_code=error.code, elapsed_seconds=round(time.monotonic()-tick, 3))
            raise
        entry.update(status='completed', elapsed_seconds=round(time.monotonic()-tick, 3))
        output = result.get('output') or []
        if any(i.get('type') not in ('function_call', 'reasoning', 'message') for i in output):
            raise fail('TOOL_DENIED', '模型返回未授权工具类型。')
        allowed = {t['function']['name'] for t in tools}
        calls, reasoning = [], []
        for item in output:
            if item['type'] == 'reasoning':
                reasoning.append(item)
            elif item['type'] == 'function_call':
                cid = item.get('call_id')
                if final or item.get('namespace') != 'cad' or item.get('name') not in allowed or not cid or cid in self.call_items:
                    raise fail('TOOL_DENIED', '模型返回未授权或重复工具调用。')
                self.call_items[cid] = item
                self.reasoning_before_call[cid] = reasoning; reasoning = []
                calls.append({'id': cid, 'type': 'function',
                    'function': {'name': item['name'], 'arguments': item['arguments']}})
        message = {'role': 'assistant', 'content': ''.join(c.get('text', '') for i in output
            if i['type'] == 'message' for c in i.get('content', []) if c.get('type') == 'output_text')}
        if calls:
            message['tool_calls'] = calls
        return message, s.usage[-1] if s.usage else {}
