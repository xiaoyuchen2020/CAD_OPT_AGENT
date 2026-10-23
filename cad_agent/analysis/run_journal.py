"""UI-only execution journal. Never enters model context or grants evidence credit.

Run summaries are small; sanitized section bodies are stored separately and read
with bounded pagination. Provider credentials and private reasoning are excluded.
"""
from contextvars import ContextVar
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import re
import time
from uuid import uuid4

from cad_agent.adapters.store import now

_wire_observer = ContextVar('cad_run_wire_observer', default=None)
MAX_SECTION_CHARS = 2_000_000
PRIVATE_KEYS = {'authorization', 'api_key', 'apikey', 'access_token', 'refresh_token',
                'id_token', 'cookie', 'set-cookie', 'encrypted_content',
                'reasoning_content', 'reasoning_text', 'chain_of_thought', 'signature',
                'password', 'client_secret', 'x-api-key', 'token', 'execution_token'}


def public_value(value):
    """Preserve application data; omit hidden reasoning and access-bearing values."""
    if isinstance(value, dict):
        if value.get('type') in ('reasoning', 'thinking', 'redacted_thinking'):
            # Only explicitly public, provider-authored summaries are retained.
            summaries = [s.get('text', '') for s in value.get('summary', [])
                         if isinstance(s, dict) and s.get('type') == 'summary_text']
            return {'type': 'reasoning_summary', 'summary': public_value(summaries),
                    'private_reasoning': 'not_recorded'}
        return {str(k): public_value(v) for k, v in value.items()
                if str(k).lower() not in PRIVATE_KEYS}
    if isinstance(value, (list, tuple)):
        return [public_value(v) for v in value]
    if isinstance(value, str):
        # Tool outputs and arguments are often JSON carried inside strings.
        if value.lstrip().startswith(('{', '[')):
            try:
                return json.dumps(public_value(json.loads(value)), ensure_ascii=False)
            except (ValueError, RecursionError):
                pass
        value = re.sub(r'data:image/[^\s"\']+', '[图像字节未记录；见图像引用]', value)
        value = re.sub(r'(?i)Bearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer [已脱敏]', value)
        value = re.sub(r'(?i)([?&](?:token|key|secret|signature|sig|code|access_token)=)[^&\s"\']+', r'\1[已脱敏]', value)
        value = re.sub(r'\bsk-[A-Za-z0-9_-]{12,}', '[已脱敏密钥]', value)
        return value
    return value


def record_wire_request(payload):
    observer = _wire_observer.get()
    if observer:
        journal, event = observer
        journal.section(event, 'request', payload, '实际模型请求（已脱敏）')
        event['capture_level'] = 'wire_payload'


class RunJournal:
    def __init__(self, session):
        self.session = session
        self.run_id = None
        self.events = []
        self.tool_stack = []
        self.model_event = None
        self.last_model_event = None

    def bind(self, run_id):
        self.run_id = run_id

    def snapshot(self, terminal=None):
        events = deepcopy(self.events)
        if terminal in ('failed', 'cancelled', 'interrupted'):
            for event in events:
                if event['status'] == 'running':
                    event['status'] = 'interrupted'
                    event['summary'] = '运行已结束；此步骤未收到完成记录。'
        return events

    def flush(self):
        if self.run_id:
            self.session.progress(run_events=self.snapshot())

    def start(self, kind, title, **fields):
        if not self.run_id:
            return None
        event = dict(id='event_' + uuid4().hex, sequence=len(self.events) + 1,
                     kind=kind, title=title, status='running', created_at=now(),
                     elapsed_seconds=round(time.monotonic()-self.session.started, 3),
                     sections={}, **public_value(fields))
        self.events.append(event)
        self.flush()
        return event

    def section(self, event, name, value, label):
        if event is None:
            return
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                pass
        clean = public_value(value)
        text = clean if isinstance(clean, str) else json.dumps(clean, ensure_ascii=False, indent=2, default=str)
        total = len(text)
        text = text[:MAX_SECTION_CHARS]
        key = 'run_section_' + uuid4().hex
        digest = hashlib.sha256(text.encode()).hexdigest()
        self.session.w.store.put('run_section', key, dict(
            id=key, run_id=self.run_id, event_id=event['id'], section=name,
            text=text, sha256=digest, original_chars=total, truncated=total > len(text)))
        event['sections'][name] = dict(id=key, label=label, chars=len(text),
                                      sha256=digest, truncated=total > len(text))

    def end(self, event, status='completed', **fields):
        if event is None:
            return
        event.update(status=status, finished_at=now(), **public_value(fields))
        event['duration_seconds'] = round(time.monotonic()-self.session.started-event['elapsed_seconds'], 3)
        self.flush()

    def model_call(self, payload, call, *, capture_level='application_input', title='模型请求', image_inputs=None):
        if not self.run_id:
            return call()
        event = self.start('model', title, capture_level=capture_level,
                           runtime=self.session.request.runtime, model=self.session.w.settings.model,
                           image_inputs=image_inputs or [])
        self.section(event, 'request', payload, '模型请求（已脱敏）')
        context_codec = getattr(self.session, '_model_context_codec', None)
        if context_codec is not None:
            from cad_agent.analysis.model_context import VERSION
            self.section(event, 'context_codec', dict(version=VERSION,
                aliases=dict(context_codec.reverse), **{'stats': context_codec.last_stats}),
                '短ID映射与编码统计（仅审计，不发送给模型）')
        self.flush()
        token = _wire_observer.set((self, event))
        previous = self.model_event
        self.model_event = event
        try:
            result = call()
            self.last_model_event = event
            self.section(event, 'response', result, '模型公开返回（不含内部思维链）')
            self.end(event)
            return result
        except Exception as error:
            self.end(event, 'failed', error_code=getattr(error, 'code', type(error).__name__),
                     summary='请求未完成；未记录原始服务错误或认证信息。')
            raise
        finally:
            self.model_event = previous
            _wire_observer.reset(token)

    @contextmanager
    def tool(self, name, arguments):
        parent = self.model_event or self.last_model_event
        event = self.start('tool', name, tool=name,
                           parent_id=parent['id'] if parent else None)
        self.section(event, 'arguments', arguments, '模型请求参数 / 调用方输入')
        self.flush()
        self.tool_stack.append(event)
        try:
            yield event
        except Exception as error:
            self.end(event, 'interrupted' if type(error).__name__ == 'ExecutionCancelled' else 'failed',
                     error_code=getattr(error, 'code', type(error).__name__))
            raise
        finally:
            self.tool_stack.pop()

    def tool_result(self, arguments, full_result, encoded, entry, archive_ref=None):
        if not self.tool_stack or self.tool_stack[-1] is None:
            return
        event = self.tool_stack[-1]
        if 'execution' not in event['sections']:
            self.section(event, 'execution', arguments, '执行参数 / 缓存查找参数')
        self.section(event, 'model_result', json.loads(encoded), '返回给运行时的结果')
        if archive_ref:
            record = self.session.w.store.get('evidence_archive', archive_ref['record_id'])
            full_result = json.loads(record['text'])
        self.section(event, 'full_result', full_result, '完整工具结果（已脱敏）')
        self.section(event, 'diagnostics', entry, '执行与结果裁剪信息')
        from cad_agent.analysis.efficiency import evidence_ids
        event.update(task_ids=entry.get('task_ids', []),
                     evidence_ids=sorted(evidence_ids(full_result) & set(self.session.evidence))[:30],
                     result_summary=public_value(entry.get('result_summary', {})))
        if name := entry.get('tool'):
            if name == 'plan_task':
                self.section(event, 'plan', self.session.task.public(), '任务计划与执行状态')

    @contextmanager
    def validation(self, content):
        event = self.start('validation', '答案与交付条件校验')
        self.section(event, 'draft', content, '待校验答案（未发布）')
        try:
            yield event
        except Exception as error:
            from cad_agent.analysis.session import validation_feedback
            self.section(event, 'validation', dict(feedback=validation_feedback(error),
                field_issues=getattr(error, 'field_issues', []),
                scope='本次程序执行的答案与交付检查；不等于工程身份或合规确认'), '校验反馈')
            self.end(event, 'failed', summary='校验未通过；该草稿未发布。')
            raise
        else:
            self.section(event, 'validation', dict(status='passed',
                scope='本次执行的答案引用、字段与交付门槛',
                limitations='未执行的几何、身份及工程检查不能由此判为通过。'), '校验结果')
            self.end(event, summary='本次交付校验通过；最终发布状态以运行状态为准。')


def read_section(workspace, run_id, event_id, section, start=0, length=12000):
    run = workspace.store.get('chat_run', run_id)
    event = next((e for e in run.get('run_events', []) if e['id'] == event_id), None)
    if not event or section not in event.get('sections', {}):
        raise KeyError(section)
    ref = event['sections'][section]
    record = workspace.store.get('run_section', ref['id'])
    if (record['run_id'], record['event_id'], record['section']) != (run_id, event_id, section):
        raise ValueError('运行记录来源不匹配。')
    text = record['text']
    if hashlib.sha256(text.encode()).hexdigest() != ref['sha256']:
        raise ValueError('运行记录完整性校验失败。')
    if start < 0 or start > len(text) or not 1 <= length <= 12000:
        raise ValueError('运行记录分页范围无效。')
    end = min(len(text), start+length)
    return dict(content=text[start:end], start=start, end=end, total_chars=len(text),
                has_more=end < len(text), next_start=end if end < len(text) else None,
                truncated=record['truncated'], original_chars=record['original_chars'],
                sha256=record['sha256'], label=ref['label'])


def export_journal(workspace, run):
    """Stream one sanitized record at a time; never export execution credentials."""
    def line(value):
        return json.dumps(public_value(value), ensure_ascii=False) + '\n'
    yield line(dict(type='run', id=run['id'], status=run['status'],
                    question=run['request']['question'], model=run.get('model'),
                    created_at=run.get('created_at'), journal_version=run.get('journal_version', 0),
                    note='公开运行记录；不含认证信息、图像字节或内部思维链。'))
    if not run.get('journal_version'):
        yield line(dict(type='legacy_trace', trace=run.get('trace', []),
                        note='旧运行未保存提示词或完整工具结果。'))
    for event in run.get('run_events', []):
        yield line(dict(type='event', event=event))
        for name, ref in event.get('sections', {}).items():
            record = workspace.store.get('run_section', ref['id'])
            if (record['run_id'], record['event_id'], record['section']) != (run['id'], event['id'], name):
                raise ValueError('运行记录来源不匹配。')
            if hashlib.sha256(record['text'].encode()).hexdigest() != ref['sha256']:
                raise ValueError('运行记录完整性校验失败。')
            yield line(dict(type='section', event_id=event['id'], section=name,
                            content=record['text'], truncated=record['truncated'],
                            original_chars=record['original_chars']))
