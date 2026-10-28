"""Model-authored query contracts; user requirements are never drawing evidence."""
from copy import deepcopy
import json
import re
import unicodedata
from typing import Literal

from pydantic import Field
from cad_agent.contracts import Record, stable_id

VERSION = 'query-intent-v1'


class QuestionBinding(Record):
    value: str = Field(min_length=1, max_length=160)
    question_quote: str = Field(min_length=1, max_length=400)
    source: Literal['current', 'parent'] = 'current'


class QueryCondition(QuestionBinding):
    operator: Literal['include', 'exclude'] = 'include'
    kind: Literal['scope', 'content'] = 'scope'


class QueryTarget(Record):
    target_id: str = Field(min_length=1, max_length=40)
    requirement_id: str
    subject: QuestionBinding
    identifier: QuestionBinding | None = None
    conditions: list[QueryCondition] = Field(default_factory=list, max_length=12)
    requested_content: list[str] = Field(default_factory=list, max_length=12)


class QueryIntent(Record):
    action: Literal['lookup', 'enumerate', 'compare', 'calculate', 'measure', 'locate', 'mixed']
    targets: list[QueryTarget] = Field(min_length=1, max_length=20)
    unresolved_requirements: list[str] = Field(default_factory=list, max_length=12)


def enabled(session):
    return getattr(session.w.settings, 'llm_query_intent', False)


def normalize(text):
    return re.sub(r'[\s—–－:：-]', '', unicodedata.normalize('NFKC', text)).casefold()


def parent_question(session):
    if not session.request.parent_run_id:
        return ''
    prior = session.w.store.get('chat_run', session.request.parent_run_id)
    request = prior.get('effective_request', prior.get('request', {}))
    selected = {request.get('snapshot_id')} | {d['snapshot_id'] for d in request.get('related_drawings', [])}
    if selected != session.allowed_snapshots:
        raise ValueError('QUERY_INTENT_HISTORY_SCOPE: parent drawing selection differs')
    return request.get('question', '')


def bind(session, value):
    question = session.request.question if value.source == 'current' else parent_question(session)
    if value.question_quote not in question or normalize(value.value) not in normalize(value.question_quote):
        raise ValueError('QUERY_INTENT_QUOTE: value/quote must be bound to an actual user question')
    return dict(source=value.source, quote=value.question_quote,
                start=question.index(value.question_quote), end=question.index(value.question_quote)+len(value.question_quote))


def register(session, raw):
    intent = QueryIntent.model_validate(raw, strict=True)
    requirement_ids = {p['id'] for p in session.task.parts}
    if {t.requirement_id for t in intent.targets} != requirement_ids:
        raise ValueError('QUERY_INTENT_REQUIREMENTS: targets must cover every original requirement_id')
    if len({t.target_id for t in intent.targets}) != len(intent.targets):
        raise ValueError('QUERY_INTENT_TARGET_ID: duplicate target_id')
    bindings = []
    for target in intent.targets:
        for value in [target.subject, target.identifier, *target.conditions]:
            if value is not None:
                bindings.append(bind(session, value))
        for condition in target.conditions:
            # A literal negative condition cannot be silently recast as inclusion.
            negative = re.search(r'(?:不要|不含|排除|除去|不包括|不包含)', condition.question_quote)
            if negative and condition.operator != 'exclude':
                raise ValueError('QUERY_INTENT_NEGATION: preserve the quoted exclusion')
    for quote in intent.unresolved_requirements:
        if quote not in session.request.question:
            raise ValueError('QUERY_INTENT_UNRESOLVED: retain actual question text')
    value = intent.model_dump()
    saved = session.task.data.get('query_intent')
    if saved and saved['intent'] != value:
        # Search relaxation is explicit on the search tool; it never rewrites the goal.
        raise ValueError('QUERY_INTENT_CHANGED: keep the registered goal; use explicit search alternatives for discovery')
    record = dict(intent_id=stable_id('intent', session.task.data['task_id'], json.dumps(value, sort_keys=True)),
                  version=VERSION, intent=value, bindings=bindings, original_question=session.request.question)
    session.task.data['query_intent'] = record
    return record


def prepare_call(session, name, args):
    raw = args.pop('query_intent', None)
    reference = args.pop('query_intent_id', None)
    if not enabled(session):
        if raw is not None or reference is not None:
            raise ValueError('QUERY_INTENT_DISABLED')
        return None
    record = register(session, raw) if raw is not None else session.task.data.get('query_intent')
    if reference and (not record or reference != record['intent_id']):
        raise ValueError('QUERY_INTENT_REFERENCE: unknown intent_id')
    if not record and name not in {'request_toolset', 'get_drawing_overview', 'read_project_sources', 'finish_analysis'}:
        raise ValueError('QUERY_INTENT_REQUIRED: first business tool call must include query_intent; preserve subject, identifier, scope and exclusions')
    return record


def tool_schema(schema):
    intent = QueryIntent.model_json_schema()
    schema.setdefault('$defs', {}).update(intent.pop('$defs', {}))
    schema['properties']['query_intent'] = intent | {'description':
        '首次业务工具调用须附带问题理解：覆盖每个requirement_id；范围/排除独立绑定用户原句。对象名称开放；不要将检索词扩展写成用户要求。后续省略以复用任务意图。'}
    schema['properties']['query_intent_id'] = {'type': 'string', 'description': '可选：复用服务端已返回的意图ID。'}


PROMPT = '''
自然语言先由你理解并选择工具。首次用search_source_records或plan_task同时提交query_intent：action及targets；每项含target_id、requirement_id、subject={value,question_quote}、可选identifier及conditions。conditions含kind=scope/content、operator=include/exclude、value、question_quote；quote必须来自用户问题。不要遗漏否定或多个对象；无法解释的原句放unresolved_requirements。历史限定可source=parent引用父问题，不能引用助手推断。
查询意图只是用户要求，不是图纸事实。具体做法编号优先search_source_records(target_id)，它只返回候选标题和真实读取参数；按next_action选择read_assembly，分页沿next_offset。范围和对象是独立约束，不把“地下室内墙4”扩展为所有地下室构造，也不把范围词和对象词强制要求出现在同一文本块。scope状态unresolved应补读来源，不能当成不存在；同号不同图框分列。确需替代词时在search_source_records显式提交alternatives、expansion_reason，不改变原始目标。已有query_intent后后续调用可省略以复用。
原文归属、编号、范围、单位与完整性必须分别检查。最终答案保持来源引用；按query_target_ids标明对应target_id，多对象必须各自回答或unknown。编号目标须引用标题证据；范围判断用evidence_roles.scope引用同图框原文，用户条件不能充当scope证据。候选、scope未核实或未读分页不能标作已完成。用户要求不授予工程身份、适用性或全图穷尽。工具不足可request_toolset；本地方法只执行你的明确调用及其必要校验。
'''


def context(session):
    return deepcopy(session.task.data.get('query_intent'))
