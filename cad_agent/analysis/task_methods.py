"""Task-local generated methods. Only host-validated pages grant evidence credit.

The worker runs synchronously inside a normal tool checkpoint: the answering
model cannot finish while it is pending. Durable attempts prevent regeneration
after cancellation/restart. Public adaptation/shadow records are never adopted.
"""
import copy
import hashlib
import time
from contextlib import contextmanager
from typing import Literal

from pydantic import Field
from cad_agent.contracts import Record
from cad_agent.adapters.model import ModelError
from cad_agent.execution import ExecutionCancelled
from cad_agent.extensions.adaptation.contracts import Family
from cad_agent.extensions.adaptation.service import AdaptationService
from cad_agent.extensions.adaptation.model import propose, check_code
from cad_agent.extensions.adaptation.sandbox import execute, runtime
from cad_agent.extensions.adaptation.validation import validate
from cad_agent.extensions.table_recovery.bundle import prepare, prepare_region, digest
from cad_agent.extensions.table_recovery.sandbox import SandboxError
from cad_agent.analysis.method_runtime import (session_supported, generation_slot,
    generation_config, usage_record, additional_charge)

TOOLS = {'develop_method', 'run_task_method'}
VERSION = 'task-verified-method@1'
PROMPT = '''
本轮开启 task_verified 动态方法。已有读取/测量工具结果无法满足具体要求时，可 develop_method：
提供已返回的 baseline_observation_id、method、具体 gap 和 instruction；范围由宿主从该次读取绑定，不能自填扩大坐标。
跨栏做法先 read_assembly；若原有结果缺少所需片段，可用 text_spans 编写局部原文选择/排序算法。
develop_method 自动生成、隔离执行、修正并验证，返回第一页及 method_id；后续页用 run_task_method。
若要求按字面词筛选并按栏位排序，必须传 text_selection={keywords:[用户原句中的词],order:"columns_top_down"}，宿主独立核对全选集及顺序后才授予selection/ranking执行回执。此排序是文字位置排序，不是数值排名。
它的 source 证据只证明所引用原文；source_binding=unconfirmed 的片段不得直接归属某做法，须明确作为局部候选。
几何结果使用工具观测及 drawing_unit，不得称为房间净面积/门宽。selection_complete只说明生成的选集已读完，不能证明目标找全。
失败时使用原工具结果或说明缺口；禁止通过更换请求绕过预算。动态方法不修改公共工具。
'''


class TextSelection(Record):
    keywords: list[str] = Field(min_length=1, max_length=6)
    order: Literal['columns_top_down'] = 'columns_top_down'


class DevelopMethod(Record):
    baseline_observation_id: str = Field(min_length=1, max_length=100)
    method: Family
    gap: str = Field(min_length=8, max_length=600)
    instruction: str = Field(min_length=8, max_length=2000)
    text_selection: TextSelection | None = None


class RunTaskMethod(Record):
    method_id: str = Field(min_length=1, max_length=100)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=5, ge=1, le=10)


@contextmanager
def borrowed_subscription(session):
    """The main Responses loop already owns an account lease (no nested lease)."""
    prior = getattr(session, '_task_method_subscription_lease', False)
    session._task_method_subscription_lease = True
    try:
        yield
    finally:
        session._task_method_subscription_lease = prior


def enabled(session):
    from cad_agent.analysis.method_authorization import authorization
    return authorization(session.request)['mode'] == 'task_verified' and session_supported(session)


def require_enabled(session):
    from cad_agent.analysis.method_authorization import authorization
    if authorization(session.request)['mode'] != 'task_verified':
        raise ValueError('TASK_METHODS_DISABLED')
    if not session_supported(session):
        raise ValueError('TASK_METHODS_RUNTIME_UNSUPPORTED')


def capture_scope(session, name, result):
    """Capture actual executed scope; model-authored gap prose is never scope."""
    if not enabled(session):
        return None
    args = session.trace[-1].get('arguments', {}) if session.trace else {}
    if name == 'read_assembly':
        sid=result.get('snapshot_id')
        if sid not in session.allowed_snapshots:return None
        from cad_agent.analysis.general import DrawingIndex
        index=session.index if sid==session.request.snapshot_id else DrawingIndex(session.w,sid)
        target = index.by_id.get(result.get('target_region_id'))
        if target is None:
            return None
        band = result.get('row_structure')
        return dict(layout_id=target.layout_id, bbox=list(band['bbox'] if band else target.bbox))
    from cad_agent.extensions.adaptation.capabilities import READ_TOOLS
    if name not in READ_TOOLS:
        return None
    if args.get('table_id'):
        return dict(table_id=args['table_id'])
    if args.get('layout_id') and args.get('bbox'):
        return dict(layout_id=args['layout_id'], bbox=list(args['bbox']))
    return None


def _ledger(session):
    return session.task.data.setdefault('task_methods', {})


def _validate(entry, bundle, raw):
    report = validate(entry['method'], bundle, raw)
    selection = entry['request'].get('text_selection')
    if report['validation_status'] != 'passed' or not selection:
        return report
    from bisect import bisect_right
    edges = entry['column_edges']
    chosen = [t for t in bundle['texts'] if any(k in t['text'] for k in selection['keywords'])]
    chosen.sort(key=lambda t:(bisect_right(edges[1:-1],t['origin'][0]),-t['origin'][1],t['origin'][0],t['entity_id']))
    expected = [dict(entity_id=t['entity_id'],start=0,end=len(t['text'])) for t in chosen]
    if raw['items'] != expected:
        report.update(validation_status='rejected',errors=['TEXT_SELECTION_OR_ORDER_MISMATCH'],items=[])
    else:
        report['text_selection_verified'] = dict(**selection, complete_in_input=True,
            ordering_basis='host_native_column_edges_and_insertion_points', target_coverage='unconfirmed')
    return report


def _check(session, entry):
    if entry['snapshot_id'] != session.request.snapshot_id or entry['version'] != VERSION:
        raise ValueError('TASK_METHOD_SOURCE_OR_VERSION_CHANGED')
    service = AdaptationService(session.w, entry['snapshot_id'])
    bundle = service.artifact('input', entry['input_artifact_id'])
    service.source_check(bundle)
    return service, bundle


class _Cancellation:
    def __init__(self, session):
        self.session = session

    def is_set(self):
        try:
            self.session._remaining()
            return False
        except (ExecutionCancelled, ModelError):
            return True


def develop(session, args):
    require_enabled(session)
    baseline = session.observations.get(args.baseline_observation_id)
    from cad_agent.extensions.adaptation.capabilities import TRIGGERS
    allowed = set(TRIGGERS[args.method]) | ({'read_assembly'} if args.method == 'text_spans' else set())
    if not baseline or baseline['snapshot_id'] != session.request.snapshot_id or baseline['tool'] not in allowed:
        raise ValueError('TASK_METHOD_BASELINE_REQUIRED')
    scope = baseline.get('task_method_scope')
    if not scope or (args.method == 'table_grid' and not scope.get('table_id')):
        raise ValueError('TASK_METHOD_EXPLICIT_READ_SCOPE_REQUIRED')
    edges = ((baseline['data'].get('row_structure') or {}).get('column_edges') or [])
    if args.text_selection:
        if args.method != 'text_spans' or not edges:
            raise ValueError('TEXT_SELECTION_REQUIRES_NATIVE_COLUMN_BASELINE')
        if any(not k.strip() or len(k)>100 or k not in session.request.question for k in args.text_selection.keywords):
            raise ValueError('TEXT_SELECTION_TERMS_REQUIRE_USER_SOURCE')
    identity = dict(task_id=session.task.data['task_id'], snapshot_id=session.request.snapshot_id,
                    method=args.method, scope=scope, version=VERSION)
    mid = 'tm_' + digest(identity)
    ledger = _ledger(session)
    # One development per method/scope/task even if the model changes its prose.
    if mid in ledger:
        return read(session, RunTaskMethod(method_id=mid))
    if len(ledger) >= 2 or len(ledger)+len(session.task.data.get('task_programs',{}))>=4:
        raise ValueError('TASK_METHOD_TASK_LIMIT')
    runtime(session.w.settings.data_dir)  # Refuse unavailable isolation BEFORE model use.
    bundle = prepare(session.index, scope['table_id']) if scope.get('table_id') else prepare_region(session.index, scope['layout_id'], scope['bbox'])
    if bundle['coverage']['blocking_diagnostics']:
        raise ValueError('INPUT_INCOMPLETE_OR_UNSUPPORTED')
    service = AdaptationService(session.w, session.request.snapshot_id)
    entry = dict(**identity, method_id=mid, status='running', request=args.model_dump(),
                 column_edges=edges,
                 baseline_observation_id=args.baseline_observation_id,
                 input_artifact_id=service.immutable('input', bundle), attempts=[], reserved_tokens=0,
                 usage=[], output_mode='task_verified', automatic_registration=False)
    ledger[mid] = entry
    session.task.save()
    deadline = time.monotonic() + min(180, max(0, session._remaining()-30))
    feedback = []
    try:
        for _ in range(3):
            session._remaining()
            seconds = min(deadline-time.monotonic(), session._remaining()-30)
            if seconds < 5:
                raise ValueError('TASK_METHOD_TIME_BUDGET')
            slot = generation_slot(session, 'TASK_METHOD_TOKEN_BUDGET')
            service.source_check(bundle)
            attempt = dict(status='generating', reserved_tokens=slot,
                           generation=generation_config(session.w.settings))
            entry['attempts'].append(attempt)
            entry['reserved_tokens'] += slot
            session.task.save()  # Unknown/failed model requests retain reservation across resume.
            session.progress(stage='动态方法 · 生成与验证', trace=session.trace, usage=session.usage)
            instruction = args.instruction
            if args.text_selection:
                import json
                instruction += '\n宿主校验契约：'+json.dumps(dict(selection=args.text_selection.model_dump(),column_edges=edges),ensure_ascii=False)
                instruction += '\n必须按keywords作字面OR筛选完整实体，以column_edges归栏，按(栏号,-origin.y,origin.x,entity_id)排序。上述栏边界是本次来源输入，不得硬编码进代码；从bundle原生线段推导一致边界。'
            proposal, meta = propose(session.w.settings, args.method, instruction, bundle, feedback,
                timeout=min(55, seconds), remaining_tokens=slot, account=session.w.siwc_account,
                lease_acquired=getattr(session, '_task_method_subscription_lease', False),
                check_cancelled=session._remaining)
            attempt['metadata_artifact_id'] = service.immutable('model', meta)
            actual = meta.get('usage', {})
            usage = usage_record(session.w.settings, actual, source='task_method', method_id=mid)
            entry['usage'].append(usage); session.usage.append(usage)
            # Reservation is a conservative charge, never reported as actual usage.
            entry['reserved_tokens'] += additional_charge(meta, slot)
            if proposal.strategy == 'abstain':
                attempt.update(status='abstained'); entry['status'] = 'unresolved'; break
            code = proposal.code
            attempt['code_artifact_id'] = service.immutable('code', dict(code=code, code_sha256=hashlib.sha256(code.encode()).hexdigest(), method=args.method))
            session.task.save()
            try:
                check_code(code)
                session._remaining()
                seconds = min(20, deadline-time.monotonic(), session._remaining()-30)
                if seconds <= 0:
                    raise ValueError('TASK_METHOD_TIME_BUDGET')
                execution = execute(session.w.settings.data_dir, bundle, code, cancel=_Cancellation(session), timeout=seconds)
                session._remaining(); service.source_check(bundle)
                attempt['execution_artifact_id'] = service.immutable('execution', execution)
                report = _validate(entry, bundle, execution['output'])
                attempt['validation_artifact_id'] = service.immutable('validation', report)
                attempt['status'] = report['validation_status']
                if report['validation_status'] == 'passed':
                    entry.update(status='validated', accepted_attempt=len(entry['attempts']))
                    break
                feedback.append(dict(code=code, errors=report['errors']))
            except (SyntaxError, ValueError, RecursionError, SandboxError) as exc:
                error = getattr(exc, 'code', 'INVALID_CODE_OR_EXECUTION')
                attempt.update(status='rejected', error=error)
                if error in ('SANDBOX_UNAVAILABLE', 'SANDBOX_CANCELLED', 'SANDBOX_ISOLATION_MISMATCH'):
                    raise
                feedback.append(dict(code=code, errors=[error]))
            session.task.save()
        if entry['status'] == 'running':
            entry['status'] = 'unresolved'
    except (ValueError, ModelError, SandboxError, ExecutionCancelled) as exc:
        entry.update(status='cancelled' if isinstance(exc, ExecutionCancelled) else 'refused',
                     error=getattr(exc, 'code', str(exc).split(':')[0])[:100])
        metadata = getattr(exc, 'generation_metadata', None)
        if metadata and entry['attempts']:
            attempt = entry['attempts'][-1]
            attempt['metadata_artifact_id'] = service.immutable('model', metadata)
            usage = usage_record(session.w.settings, metadata.get('usage'), source='task_method', method_id=mid)
            entry['usage'].append(usage); session.usage.append(usage)
            entry['reserved_tokens'] += additional_charge(metadata, attempt['reserved_tokens'])
        if isinstance(exc, ExecutionCancelled):
            raise
    finally:
        if entry['status'] == 'running':
            entry.update(status='interrupted', error='INTERRUPTED_NO_AUTOMATIC_REGENERATION')
        if entry['attempts'] and entry['attempts'][-1]['status'] == 'generating':
            entry['attempts'][-1].update(status=entry['status'], error=entry.get('error'))
        session.task.save()
    return read(session, RunTaskMethod(method_id=mid))


def _text_evidence(session, bundle, item):
    """Rebuild exact selected spans; don't give credit to unselected entity tails."""
    source = next(t for t in bundle['texts'] if t['entity_id'] == item['entity_id'])
    result = []
    for region, part in session.index.entity_sources.get(item['entity_id'], []):
        start, end = part.text_span
        if region.text[start:end] != source['text']:
            continue
        result.append(session.index.evidence(region, start+item['start'], start+item['end']))
    if not result or any(e['quote'] != item['quote'] for e in result):
        raise ValueError('TASK_METHOD_EXACT_SPAN_UNAVAILABLE')
    return result


def read(session, args):
    require_enabled(session)
    entry = _ledger(session).get(args.method_id)
    if not entry:
        raise ValueError('TASK_METHOD_NOT_OWNED_BY_TASK')
    service, bundle = _check(session, entry)
    if entry['status'] != 'validated':
        return dict(method_id=args.method_id, status='unresolved', reason=entry.get('error', entry['status']),
                    evidence=[], admitted=False, note='未获得可引用动态结果；继续已有来源或说明缺口。')
    attempt = entry['attempts'][entry['accepted_attempt']-1]
    code = service.artifact('code', attempt['code_artifact_id'])
    execution = service.artifact('execution', attempt['execution_artifact_id'])
    stored = service.artifact('validation', attempt['validation_artifact_id'])
    # Revalidate against CURRENT trusted validator and source on every read/resume.
    report = _validate(entry, bundle, execution['output'])
    if report != stored or report['validation_status'] != 'passed':
        raise ValueError('TASK_METHOD_VALIDATION_CHANGED')
    rows = report.get('cells', []) if entry['method'] == 'table_grid' else report['items']
    if args.offset > len(rows):
        raise ValueError('TASK_METHOD_INVALID_OFFSET')
    selected = copy.deepcopy(rows[args.offset:args.offset+args.limit])
    evidence = []
    if entry['method'] == 'text_spans':
        for item in selected:
            item['evidence'] = _text_evidence(session, bundle, item)
            evidence.extend(item['evidence'])
    # Table output is a structural observation, NOT a blanket source citation.
    if entry['method'] == 'table_grid':
        for item in selected:
            item.pop('evidence', None)
    out = dict(method_id=args.method_id, status='validated', kind='task_method_result', admitted=True,
        method=entry['method'], snapshot_id=entry['snapshot_id'], input_id=bundle['input_id'],
        source_sha256=bundle['source_sha256'], code_sha256=code['code_sha256'],
        validator_version=report['validator_version'], validation_artifact_id=attempt['validation_artifact_id'],
        baseline_observation_id=entry['baseline_observation_id'], items=selected, evidence=evidence,
        offset=args.offset, total=len(rows), has_more=args.offset+len(selected)<len(rows),
        source_binding=dict(state='candidate', basis='validated_source_only', attribution='unconfirmed'),
        semantic_acceptance=False, target_coverage='unconfirmed',
        selection_complete=args.offset == 0 and len(selected) == len(rows),
        unit='drawing_unit_squared' if entry['method']=='closed_contour' else 'drawing_unit',
        note='验证仅覆盖原文/原生几何及计算；选集不证明完整，来源内归属与工程身份另核对。')
    if out['has_more']:
        out['next_call'] = dict(tool='run_task_method', arguments=dict(method_id=args.method_id, offset=args.offset+len(selected)))
    if report.get('text_selection_verified'):
        out['text_selection_verified'] = report['text_selection_verified']
        # These receipts prove only literal selection and position ordering.
        out['operation_receipts'] = [dict(operation=op, fulfilled=True, tool='run_task_method',
            reference=args.method_id, basis='independent_literal_selection_and_native_position',
            engineering_identity=False) for op in ('selection','ranking')]
    out['claim_templates'] = [dict(text=e['quote'], state='observed', evidence_basis='source',
        evidence_ids=[e['evidence_id']]) for e in evidence]
    unbound = session.task.data.setdefault('task_method_unbound_evidence', {})
    for e in evidence:
        # An already-read source keeps its prior authority. New source fragments
        # prove literal text only until a native reader establishes attribution.
        if e['evidence_id'] not in session.evidence:
            unbound[e['evidence_id']] = args.method_id
    # All validation is complete before granting any evidence IDs.
    return session._register_tree(out)


def claim_issues(session, claim):
    """Generated output never gets a generic numeric/semantic escape hatch."""
    unbound = session.task.data.get('task_method_unbound_evidence', {})
    touched = set(claim.evidence_ids) & unbound.keys()
    if touched:
        for mid in {unbound[eid] for eid in touched}:
            _check(session, _ledger(session)[mid])
        # First-stage admission is exact source reporting, not a general
        # semantic association validator. Native independently-bound evidence
        # can support richer answers through the existing read_assembly path.
        literal = any(claim.text == session.evidence[eid]['quote'] for eid in touched)
        if claim.state != 'unknown' and not literal:
            return ['动态提取片段尚未确认做法归属；按claim_templates逐字引用，或保留unknown候选并补读归属依据。']
    dynamic = [session.observations[i]['data'] for i in claim.observation_ids
               if i in session.observations and session.observations[i]['tool'] in TOOLS]
    if not dynamic:
        return []
    if any(not d.get('admitted') for d in dynamic):
        return ['动态方法未验证通过，不能作为回答证据。']
    for d in dynamic:
        _check(session, _ledger(session)[d['method_id']])
    if claim.state not in ('derived', 'unknown'):
        return ['动态方法观测必须保留 derived/unknown 状态。']
    if any(d['method']=='text_spans' for d in dynamic) and claim.state != 'unknown':
        return ['原文内容必须引用动态方法返回的 source evidence_ids，不能用观测替代。']
    import re
    if re.search(r'净面积|房间面积|墙厚|门宽|平方米|毫米|全部|总计|完整', claim.text):
        return ['动态方法仅核验局部原生几何/结构；不能据此确认工程身份、物理单位或完整总量。']
    return []


def verified_position_delivery(data, step, cell, claims):
    """Discharge a POSITION delivery cell only from a fully published selection.

    This is not source attribution. All selected exact quotes must be present,
    in verified order, and the user-bound predicate/order must belong to this
    requirement. Unrelated output-format cells and partial pages stay pending.
    """
    if cell.get('requirement_type') != 'DELIVERY_REQUIREMENT':
        return None
    text = step['requirement']
    if '从左到右' not in text or '从上到下' not in text:
        return None
    if not {'selection','ranking'} <= set(step.get('operations', [])):
        return None
    for oid in step.get('observation_ids', []):
        observation = data.get('observations', {}).get(oid, {})
        result = observation.get('data', {})
        proof = result.get('text_selection_verified', {})
        if (observation.get('tool') not in TOOLS or not result.get('admitted') or not result.get('selection_complete')
                or not proof.get('complete_in_input') or proof.get('order') != 'columns_top_down'
                or not all(k in text for k in proof.get('keywords', []))):
            continue
        expected = [(e['evidence_id'], e['quote']) for e in result.get('evidence', [])]
        actual = [(eid, c['text']) for c in claims if c['state'] != 'unknown' and c.get('evidence_basis') == 'source'
                  for eid in c.get('evidence_ids', [])]
        if expected and actual == expected:
            return dict(observation_id=oid, method_id=result['method_id'],
                        basis='exact_published_selection_and_native_position', source_attribution=False)
    return None
