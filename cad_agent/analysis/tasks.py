"""Persistent delivery ledger shared by runtimes. Source IDs stay server-owned.

Planning organizes requirements; it cannot mark work complete or invent evidence.
Unit confirmation resumes local measurements without an implicit inference call.
"""
from copy import deepcopy
import json
import re
import math
from shapely.geometry import Polygon
from cad_agent.geometry.source import UNIT_TO_M
from typing import Literal
from uuid import uuid4

from pydantic import Field
from cad_agent.contracts import Record
from cad_agent.adapters.store import now
from cad_agent.analysis.operations import Operation, infer_operations, planned_operations, receipts
from cad_agent.analysis.coverage import question_parts
from cad_agent.analysis.unit_policy import user_unit_policy
from cad_agent.geometry.room_batch import RoomBatchService, RoomBatchQuery, UnitConfirmation, ScopeConfirmation

VERSION = 'task-ledger-v2'
TERMINAL = {'completed', 'waiting_input', 'blocked_capability', 'partial'}


def natural_parts(question):
    explicit = question_parts(question)
    if explicit:
        return explicit
    # Preserve every source clause verbatim, including shared-scope clauses.
    # This is a lexical draft; the model supplies dependencies and capabilities.
    chunks = re.split(r'(?<=[。？?；;！!])\s*|[,，]\s*(?=(?:并且|并|同时|另外|再|然后|以及|还请|还要|测量|计算|读取|核对|比较|高亮|列出))|(?<=\S)(?=并(?:帮我|请|高亮|标出|比较|列出))', question)
    chunks = [x.strip() for x in chunks if x.strip()]
    merged=[]
    for chunk in chunks:
        if merged and merged[-1] in ('核查','检查','查看','识别','计算','测量','定位') and chunk.startswith('并'):
            merged[-1]+=chunk
        else:merged.append(chunk)
    chunks=merged
    if len(chunks) > 8:
        chunks = chunks[:7]+[''.join(chunks[7:])]
    return [{'id':f'q{i+1}', 'text':t} for i,t in enumerate(chunks)]


def draft_kind(text):
    if user_unit_policy(text) and not re.search('哪些|列出|面积|高亮|轮廓|计算|多少',text): return 'constraint'
    if any(x in text for x in ('比较', '对比', '关联', '综合', '判断')): return 'compare'
    if any(x in text for x in ('面积', '测量', '长度', '距离')): return 'measure'
    if any(x in text for x in ('高亮', '标出', '轮廓', '边界')): return 'highlight'
    if any(x in text for x in ('哪些', '列出', '枚举', '所有')): return 'enumerate'
    return 'lookup'


class PlanStep(Record):
    requirement_id: str
    operations: list[Operation] = Field(default_factory=list, max_length=16, description="仅列用户要求的操作：discovery专指不依赖名称的几何空间发现；contours轮廓核查；selection筛选集合；units单位检查；dimensions专指读取原图尺寸标注/尺寸链，测几何长度宽度间距用geometry。area/perimeter/arithmetic/ranking/relations/vision/highlight/exclusions按需。勿给命名房间枚举添加discovery。")
    kind: Literal['scope', 'enumerate', 'measure', 'lookup', 'compare', 'highlight', 'constraint']
    depends_on: list[str] = Field(default_factory=list, max_length=8)
    acceptance: str = Field(min_length=1, max_length=300)
    actions: list[str] = Field(default_factory=list, max_length=8, description='为满足目标执行的步骤，可包括范围、空间识别、单位检查、测量及复核；不是新增用户要求。')


class TaskPlan(Record):
    steps: list[PlanStep] = Field(min_length=1, max_length=10)


class TaskUnitConfirmation(UnitConfirmation):
    batch_id: str


class TaskBatchNavigation(Record):
    batch_id: str
    offset: int | None = Field(default=None, ge=0)
    frame_id: str | None = None


class TaskLedger:
    def __init__(self, w, request):
        self.w = w
        drawing = w.store.get('drawing', request.snapshot_id)
        if request.resume_task_id:
            self.data = self.read(w, request.resume_task_id)
            if self.data['snapshot_id'] != request.snapshot_id or self.data['question'] != request.question:
                raise ValueError('继续任务必须使用原图纸和原始问题。')
            if self.data.get('related_drawings',[]) != [d.model_dump() for d in request.related_drawings]:
                raise ValueError('继续任务必须保持原关联图纸清单。')
            if self.data['status'] == 'running':
                raise ValueError('任务仍在运行，不能同时继续。')
            for step in self.data['subtasks']:
                if step['status'] in ('pending','running','partial','blocked_capability'):
                    step.update(status='pending',calls=0,stalled=0)
            self.data.pop('checkpoint',None)
        else:
            parts = natural_parts(request.question)
            self.data = dict(task_id='task_'+uuid4().hex, version=VERSION, snapshot_id=request.snapshot_id,
                source_sha256=drawing['sha256'], related_drawings=[d.model_dump() for d in request.related_drawings], question=request.question, created_at=now(), revision=0,
                status='pending', planned=False, plan_origin=None, run_ids=[],
                publication_status='not_published',unit_policy=user_unit_policy(request.question),
                subtasks=[dict(id=p['id'], requirement=p['text'], kind=draft_kind(p['text']), depends_on=[],
                    acceptance='回答该要求，或明确缺失依据；候选不能升级为确认事实。', status='pending',
                    reason=None, evidence_ids=[], observation_ids=[], batch_ids=[], calls=0, stalled=0,
                    delivered=False, claims=[], operations=infer_operations(p['text']), operation_receipts=[]) for p in parts],
                batches={}, evidence={}, observations={}, delivery_check=None)

    @staticmethod
    def read(w, task_id):
        d = w.store.get('agent_task', task_id)
        drawing = w.store.get('drawing', d['snapshot_id'])
        if d['source_sha256'] != drawing['sha256']:
            raise ValueError('任务来源版本已改变，不能复用旧证据。')
        return d

    @property
    def complex(self): return len(self.data['subtasks']) > 1

    @property
    def needs_plan(self): return self.complex and not self.data['planned']

    @property
    def parts(self): return [{'id':s['id'], 'text':s['requirement']} for s in self.data['subtasks']]

    def save(self):
        from cad_agent.execution import ExecutionCancelled
        import time
        token = self.data.get('execution_token')
        if token:
            # Keep heartbeat renewal fields from the latest row and fence stale workers.
            with self.w.execution.transaction() as c:
                current=self.w.execution.read(c,'agent_task',self.data['task_id'])
                if current.get('execution_token')!=token or current.get('execution_lease_until',0)<time.time():
                    raise ExecutionCancelled('任务执行租约已失效。')
                self.data['execution_lease_until']=current['execution_lease_until']
                self.data['revision']=current.get('revision',0)+1
                self.data['updated_at']=now()
                self.w.execution.write(c,'agent_task',self.data['task_id'],self.data)
        else:
            revision=self.data.get('revision',0)
            self.data['revision']=revision+1
            self.data['updated_at']=now()
            try:
                saved=self.w.store.compare_and_swap('agent_task',self.data['task_id'],
                    {'revision':revision,'execution_token':None},self.data)
            except KeyError:
                self.w.store.put('agent_task',self.data['task_id'],self.data)
            else:
                if saved is None:
                    raise ExecutionCancelled('任务已被其他执行器更新，请刷新后继续。')

    def public(self):
        from cad_agent.analysis.object_registry import registry_summary
        return {k:deepcopy(v) for k,v in self.data.items() if k not in ('evidence','observations','batches','object_registry','object_workflows')} | ({'object_registry':registry_summary(self.data)} if self.data.get('object_registry') else {}) | {
            'batch_refs':[dict(batch_id=b['batch_id'],status=b['status'],evidence_url=b['evidence_url']) for b in self.data['batches'].values()],
            'evidence_url':'/api/tasks/'+self.data['task_id'],
            'note':'完成表示交付项有记录，不代表候选对象或设计语义已得到人工确认。'}

    def context(self):
        d=self.public()
        d.pop('spatial_results',None)
        d['subtasks']=[{k:v for k,v in s.items() if k not in ('claims','evidence_ids','observation_ids')} for s in d['subtasks']]
        return d

    def plan(self, args, origin='model'):
        if self.data['planned']:
            raise ValueError('已有计划；本轮不允许重复规划或删除已完成要求。')
        nodes = {s['id']:s for s in self.data['subtasks']}
        if len(args.steps) != len(nodes) or {s.requirement_id for s in args.steps} != set(nodes):
            raise ValueError('计划必须逐一覆盖所有原始 requirement_id，不得增删或重复。')
        edges = {s.requirement_id:set(s.depends_on) for s in args.steps}
        if any(not deps <= set(nodes) or key in deps for key,deps in edges.items()):
            raise ValueError('依赖必须引用本任务其他要求。')
        done=set()
        while len(done)<len(nodes):
            ready={k for k,v in edges.items() if v<=done}-done
            if not ready: raise ValueError('任务依赖存在循环。')
            done |= ready
        for p in args.steps:
            kind='constraint' if draft_kind(nodes[p.requirement_id]['requirement'])=='constraint' else p.kind
            nodes[p.requirement_id].update(kind=kind, depends_on=p.depends_on, acceptance=p.acceptance,actions=p.actions,
                operations=planned_operations(nodes[p.requirement_id]['requirement'],p.operations))
        self.data.update(planned=True,plan_origin=origin)
        self.save()
        return self.public()

    def auto_plan(self):
        if not self.data['planned']:
            self.plan(TaskPlan(steps=[PlanStep(requirement_id=s['id'],kind=s['kind'],acceptance=s['acceptance'])
                                     for s in self.data['subtasks']]),'local_batch_workflow')

    def select(self, ids):
        by_id = {s['id']:s for s in self.data['subtasks']}
        if not ids or len(ids)!=len(set(ids)) or not set(ids)<=set(by_id):
            raise ValueError('必须使用任务中的 task_ids 关联本次工具调用。')
        if self.needs_plan: raise ValueError('先调用 plan_task 确认完整计划。')
        for key in ids:
            s=by_id[key]
            if self.complex and s['status']=='completed' and s['delivered']:
                raise ValueError('该分项已经交付，请复用已有来源，继续未完成要求。')
            if s['status'] in ('waiting_input','blocked_capability'):
                raise ValueError('该子任务已阻塞，请执行其他可推进要求或等待确认。')
            if s['calls']>=12: raise ValueError('该子任务工具预算已用完，请交代已有结果与缺口。')
            if any(by_id[d]['status'] in ('waiting_input','blocked_capability') and by_id[d]['kind']!='constraint' for d in s['depends_on'] if d not in ids):
                raise ValueError('该子任务依赖尚未解除；请继续独立子任务。')
        return [by_id[k] for k in ids]

    def start(self, ids):
        for s in self.select(ids):
            s.update(status='running',calls=s['calls']+1)
        self.save()

    def record(self, ids, name, result, evidence, observations, batch=None, duplicate=False):
        self.data['evidence'].update(deepcopy(evidence))
        self.data['observations'].update(deepcopy(observations))
        eid=set(evidence); oid=result.get('observation_id')
        for s in self.data['subtasks']:
            if s['id'] not in ids: continue
            s['evidence_ids']=sorted(set(s['evidence_ids'])|eid)
            if oid and oid not in s['observation_ids']:s['observation_ids'].append(oid)
            s['stalled']=s['stalled']+1 if duplicate else 0
            s['status']='pending'
            if s['stalled']>=2 or s['calls']>=12:
                s.update(status='blocked_capability',reason='subtask_tool_budget_or_repetition')
        for s in self.data['subtasks']:
            if s['id'] in ids:
                s.setdefault('operation_receipts',[]).extend(receipts(name,result))
        if batch:
            # A new page changes the candidate set for every requirement owned
            # by this same batch query, including measurement waiting on units.
            related=set(ids)
            replaced=batch.get('parent_batch_id') if batch.get('boundary_review_id') or batch.get('unit_resolution') else None
            if replaced in self.data['batches']:
                for step in self.data['subtasks']:
                    if replaced in step['batch_ids']:
                        related.add(step['id'])
                        step['batch_ids'].remove(replaced)
                self.data['batches'].pop(replaced)
            if batch['scope']:
                for step in self.data['subtasks']:
                    if any((old:=self.data['batches'].get(bid)) and old['scope'] and
                           old['scope']['frame_id']==batch['scope']['frame_id'] and
                           old['request']['room_label']==batch['request']['room_label'] for bid in step['batch_ids']):
                        related.add(step['id'])
                        step['evidence_ids']=sorted(set(step['evidence_ids'])|eid)
                        if oid and oid not in step['observation_ids']:step['observation_ids'].append(oid)
            self.data['batches'][batch['batch_id']]=deepcopy(batch)
            self.bind_batch(related,batch)
        self.save()

    def bind_batch(self, ids, batch):
        for s in self.data['subtasks']:
            if s['id'] not in ids or s['kind'] not in ('scope','enumerate','measure','highlight','constraint'):continue
            if s['kind']=='constraint':
                if not user_unit_policy(s['requirement']):continue
                resolution=batch.get('unit_resolution')
                if resolution:
                    if batch['batch_id'] not in s['batch_ids']:s['batch_ids'].append(batch['batch_id'])
                    resolved=resolution['status']=='resolved'
                    s.update(status='completed' if resolved else 'waiting_input',reason=resolution['reason'],delivered=True)
                continue
            if batch['batch_id'] not in s['batch_ids']:s['batch_ids'].append(batch['batch_id'])
            # Aggregate only pages of the same source scope and label. Keep
            # unknown-unit pages pending even when another page was confirmed.
            pages=[self.data['batches'][bid] for bid in s['batch_ids'] if bid in self.data['batches']]
            groups={(b['scope']['frame_id'] if b['scope'] else None,b['request']['room_label']) for b in pages}
            rows_by_id={i['region_id']:i for b in pages for i in b['items']}
            rows=list(rows_by_id.values())
            complete=(len(groups)==1 and len(rows)==batch['total'] and
                      {i['number'] for i in rows}==set(range(1,batch['total']+1)))
            status,reason='partial','候选范围或分页未完整覆盖。'
            if not batch['scope']: status,reason='waiting_input','需要确认有独立边界的楼层范围。'
            elif s['kind']=='scope': status,reason='completed','图框与标题候选已定位，适用范围仍需复核。'
            elif s['kind']=='measure':
                if any(b['unit']=='unknown' for b in pages): status,reason='waiting_input','unknown_units'
                elif complete and rows and all(i['status']=='measured' for i in rows):status,reason='completed','逐项净面积候选已计算；对象身份仍需复核。'
                elif complete and rows and all(i.get('candidate_area_m2') is not None for i in rows):status,reason='partial','已计算所选轮廓面积；净面积口径尚未确认。'
                else:status,reason='partial','存在未测量、待复核或未覆盖的候选；不能确认全部净面积。'
            elif s['kind']=='enumerate' and complete:status,reason='completed','匹配名称候选已逐项列出；不等于确认房间总数。'
            elif s['kind']=='highlight':
                status='completed' if complete and rows and all(i['highlight']['kind']=='candidate_boundary' for i in rows) else 'partial'
                reason='已生成逐项候选轮廓高亮。' if status=='completed' else '部分候选尚无有效轮廓，保留名称定位。'
            # Boundary review is independent of physical units. A label-only
            # batch must not prematurely complete or globally stop that work.
            boundary_required=s['kind'] in ('measure','highlight') or bool(re.search('轮廓|边界|床框',self.data['question']))
            reviewed=all(b.get('boundary_review_id') or (b['items'] and all(i['status']=='measured' for i in b['items'])) for b in pages)
            if boundary_required and batch['scope'] and batch['items'] and not reviewed:
                s.update(status='pending',reason='还需局部看图选择并校验候选边界。',delivered=False)
            elif s['kind']=='measure' and any(b['unit']=='unknown' and not b.get('unit_resolution') for b in pages):
                s.update(status='pending',reason='还需检查图纸单位与用户测量约定。',delivered=False)
            else:
                s.update(status=status,reason=reason,delivered=True)
            if self.missing_operations(s):
                s.update(status='pending',delivered=False,reason='尚有未完成操作：'+','.join(self.missing_operations(s)))

    def missing_operations(self, step, attempted=False):
        seen={r['operation'] for r in step.get('operation_receipts',[]) if attempted or r.get('fulfilled')}
        return sorted(set(step.get('operations',[]))-seen)

    def pending_work(self):
        """Missing operations, not missing prose; reusable by every runtime."""
        work=[]
        for b in self.data['batches'].values():
            owners=[s for s in self.data['subtasks'] if b['batch_id'] in s['batch_ids']]
            if not b['scope'] or not b['items']:continue
            if any(s['kind'] in ('measure','highlight') for s in owners) and not b.get('boundary_review_id') and not all(i['status']=='measured' for i in b['items']):
                work.append(dict(operation='boundary_review',batch_id=b['batch_id'],task_ids=[s['id'] for s in owners if s['status']=='pending']))
            if any(s['kind']=='measure' for s in owners) and b['unit']=='unknown' and not b.get('unit_resolution'):
                work.append(dict(operation='resolve_units',batch_id=b['batch_id'],task_ids=[s['id'] for s in owners if s['kind']=='measure' and s['status']=='pending']))
        for s in self.data['subtasks']:
            if s['status'] not in ('waiting_input','blocked_capability'):
                work.extend(dict(operation=op,task_ids=[s['id']],attempted=False) for op in self.missing_operations(s,attempted=True))
        return work

    def runnable(self):
        nodes={s['id']:s for s in self.data['subtasks']}
        return [s['id'] for s in nodes.values() if s['status'] in ('pending','running') and
                not any(nodes[d]['status'] in ('waiting_input','blocked_capability') and nodes[d]['kind']!='constraint' for d in s['depends_on'])]

    def finalise(self, result):
        checks=[]
        outcomes={v["requirement_id"]:v for v in result.get("requirement_outcomes",[])}
        for s in self.data['subtasks']:
            claims=[c for c in result['claims'] if s['id'] in c.get('question_part_ids',[])]
            if not self.complex:claims=result['claims']
            if claims:s['claims']=deepcopy(claims)
            if claims and not s['batch_ids']:
                supported=all(c['state']!='unknown' and (c.get('evidence_ids') or c.get('observation_ids')) for c in claims)
                s.update(status='completed' if supported else 'blocked_capability',delivered=True,
                    reason='已交付有引用的结论，语义需复核。' if supported else '已明确说明现有依据不足。')
            missing=self.missing_operations(s)
            if not missing and s.get('operations') and claims and all(c['state']!='unknown' for c in claims):
                s.update(status='completed',delivered=True,reason='所需操作均有工具结果；候选身份与计量口径保留。')
            if missing:
                s.update(status='partial',reason='尚未满足：'+','.join(missing),delivered=bool(claims) or s['delivered'])
            outcome=outcomes.get(s['id'])
            if outcome:
                s['gap_kind']=outcome.get('gap_kind','none')
                if outcome['status']!='fulfilled':s.update(status='partial',reason=outcome['reason'])
            # An unavailable target measurement does not undo an applied unit
            # convention. Only source-owned resolved units discharge this
            # narrow constraint; arbitrary claims and unknown units cannot.
            policy=user_unit_policy(s['requirement']) if s['kind']=='constraint' else None
            if policy and policy['condition']=='explicit' and set(s.get('operations',[]))<={'units'}:
                applied=[o['data'].get('applied_unit') for o in self.data['observations'].values()
                         if o['tool'] in ('review_space_connections','review_discovered_spaces','check_dimension_chain')
                         and o['data'].get('applied_unit')]
                applied += [b['unit'] for b in self.data['batches'].values() if b.get('unit_resolution',{}).get('status')=='resolved']
                if applied and set(applied)=={policy['unit']}:
                    s.setdefault('operation_receipts',[]).append(dict(operation='units',fulfilled=True,basis='source_measurement_units'))
                    s.update(status='completed',delivered=True,reason='实际几何结果已采用指定单位。',gap_kind='none')
                    missing=self.missing_operations(s)
            checks.append(dict(requirement_id=s['id'],delivered=bool(claims) or s['delivered'],status=s['status'],missing_operations=missing))
        self.check_batch_rows()
        if any(not c['delivered'] for c in checks):
            raise ValueError('未交代所有任务要求：'+','.join(c['requirement_id'] for c in checks if not c['delivered']))
        self.data['delivery_check']={'all_requirements_addressed':True,'items':checks,
            'note':'逐项交付检查不证明语义正确；待确认和部分完成不是整体完成。'}
        self.refresh_status()
        self.save()
        return self.public()

    def check_batch_rows(self):
        # Batch outputs cannot silently omit individual rows or turn pending
        # measurements into zeros. Each saved page is a separate object set.
        shapes={}
        for b in self.data['batches'].values():
            expected=min(b['request']['limit'],max(0,b['total']-b['request']['offset'])) if b['scope'] else 0
            if len(b['items'])!=expected or len({i['region_id'] for i in b['items']})!=len(b['items']):
                raise ValueError('房间批次逐项交付不完整或存在重复项。')
            if any(not i['label_evidence'] or not i.get('highlight') or (i['status']=='needs_unit_confirmation' and
                    (i['area_m2'] is not None or i['candidate_area_m2'] is not None)) for i in b['items']):
                raise ValueError('房间候选缺少证据/高亮或单位未知时出现面积数值。')
            for item in b['items']:
                h=item['highlight']
                if h['kind']!='candidate_boundary':continue
                p=Polygon(h['boundary'],h.get('holes',[]))
                if not p.is_valid or p.is_empty or p.area<=0:raise ValueError('房间高亮不是有效闭合多边形。')
                area=item.get('area_m2') if item.get('area_m2') is not None else item.get('candidate_area_m2')
                if area is not None:
                    factor=UNIT_TO_M.get(b['unit'])
                    if not factor or not math.isclose(area,p.area*factor**2,rel_tol=1e-6,abs_tol=1e-5):
                        raise ValueError('面积与高亮轮廓或单位不一致，需重新测量。')
                group=(b['scope']['layout_id'],b['scope']['frame_id'],b['request']['room_label'])
                shapes.setdefault(group,{})[item['region_id']]=p
        for group in shapes.values():
            polygons=list(group.values())
            for n,p in enumerate(polygons):
                if any(p.intersection(q).area>min(p.area,q.area)*1e-6 for q in polygons[n+1:]):
                    raise ValueError('不同房间候选的内部轮廓重叠，不能作为独立房间交付。')

    def refresh_status(self):
        states={s['status'] for s in self.data['subtasks']}
        self.data['status']='completed' if states=={'completed'} else 'waiting_input' if 'waiting_input' in states else 'partial'
        if self.data.get('publication_status')=='failed':self.data['status']='partial'

    def publication_failed(self, error):
        # A local checkpoint is never a model answer or an accepted conclusion.
        # Preserve exact source fragments and deterministic outputs separately.
        ids=list(dict.fromkeys(e for s in self.data['subtasks'] for e in s['evidence_ids'][-2:]))
        self.data['checkpoint']={
            'kind':'local_saved_evidence','not_a_final_answer':True,
            'sources':[deepcopy(self.data['evidence'][e]) for e in ids if e in self.data['evidence']],
            'calculations':[deepcopy(o) for o in self.data['observations'].values() if o['tool'] in ('calculate_facts','analyze_measurements','check_dimension_chain')],
            'pending_source_reads':self.data.get('source_read_queue',[]),
            'note':'已读原文及计算记录；尚未完成适用关系和整体答案复核。可恢复继续。'}
        self.data.update(publication_status='failed',last_error=error)
        self.refresh_status()
        self.save()

    @classmethod
    def confirm_unit(cls,w,task_id,args):
        # Serialize confirmations and session acquisition with the workspace lock.
        with w.lock:
            data=cls.read(w,task_id)
            if data['status']=='running':raise ValueError('任务仍在运行，请等待回答后确认单位。')
            previous=data.get('unit_resumptions',{}).get(args.batch_id)
            if previous:
                if previous['unit']!=args.unit:raise ValueError('该确认已用于其他单位，请建立新的测量任务。')
                ledger=cls.__new__(cls);ledger.w=w;ledger.data=data
                return {'task':ledger.public(),'room_batches':list(data['batches'].values()),'model_calls':0,'note':'已应用该单位确认，没有重复测量。'}
            if args.batch_id not in data['batches']:raise ValueError('批次不属于当前任务。')
            old=data['batches'][args.batch_id]
            service=RoomBatchService(w,data['snapshot_id'])
            new=service.confirm(args.batch_id,UnitConfirmation(unit=args.unit,confirmed=True))
            ledger=cls.__new__(cls);ledger.w=w;ledger.data=data
            ids=[s['id'] for s in data['subtasks'] if args.batch_id in s['batch_ids']]
            del data['batches'][args.batch_id];data['batches'][new['batch_id']]=new
            for s in data['subtasks']:
                s['batch_ids']=[bid for bid in s['batch_ids'] if bid!=args.batch_id]
            ledger.bind_batch(ids,new)
            # Superseded observations must not support a resumed answer.
            ancestors={args.batch_id};cursor=old
            while cursor.get('parent_batch_id') and cursor['parent_batch_id'] not in ancestors:
                ancestors.add(cursor['parent_batch_id'])
                try:cursor=service.get(cursor['parent_batch_id'])
                except KeyError:break
            stale={oid for oid,o in data['observations'].items() if o.get('data',{}).get('batch_id') in ancestors}
            for oid in stale:data['observations'].pop(oid)
            changed={s['id'] for s in data['subtasks'] if s['id'] in ids and s['kind'] in ('measure','highlight')}
            for s in data['subtasks']:
                s['observation_ids']=[o for o in s['observation_ids'] if o not in stale]
                if s['id'] in changed:s['claims']=[]
                else:s['claims']=[c for c in s['claims'] if not set(c.get('observation_ids',[])) & stale]
            # Invalidate transitive dependent conclusions, not unrelated reads.
            for _ in data['subtasks']:
                for s in data['subtasks']:
                    if set(s['depends_on'])&changed and s['id'] not in changed:
                        changed.add(s['id']);s.update(status='pending',delivered=False,claims=[],reason='上游测量已变化，需要重新综合。',calls=0,stalled=0)
            ledger.check_batch_rows()
            data['delivery_check']={'all_requirements_addressed':all(s['delivered'] for s in data['subtasks']),
                'items':[dict(requirement_id=s['id'],delivered=s['delivered'],status=s['status']) for s in data['subtasks']],
                'note':'本地续作已逐项检查；历史模型回答不随单位确认自动改写。'}
            data.setdefault('unit_resumptions',{})[args.batch_id]={'unit':args.unit,'batch_id':new['batch_id']}
            from cad_agent.analysis.object_registry import record_local_batch
            record_local_batch(w,data,new)
            ledger.refresh_status();ledger.save()
            return {'task':ledger.public(),'room_batches':list(data['batches'].values()),'model_calls':0,
                'note':'单位确认后已恢复本地测量。此前模型回答为历史记录；以此任务状态和最新测量清单为准。剩余综合分析可显式继续。'}

    @classmethod
    def navigate_batch(cls,w,task_id,args):
        """Local continuation; IDs and scope choices are verified by the server."""
        with w.lock:
            data=cls.read(w,task_id)
            if data['status']=='running':raise ValueError('任务仍在运行。')
            if args.batch_id not in data['batches']:raise ValueError('批次不属于当前任务。')
            old=data['batches'][args.batch_id];service=RoomBatchService(w,data['snapshot_id'])
            if (args.offset is None)==(args.frame_id is None):raise ValueError('指定分页或范围其中一项。')
            if args.frame_id is not None:
                if old['status']!='needs_scope_confirmation':raise ValueError('该批次不需要确认范围。')
                choices={f['frame_id']:f for f in old['scope_choices']}
                if args.frame_id not in choices:raise ValueError('范围不属于此批次候选。')
                new=service.confirm_scope(args.batch_id,ScopeConfirmation(frame_id=args.frame_id,confirmed=True))
                del data['batches'][args.batch_id]
            else:
                if not old['scope'] or args.offset>=old['total']:raise ValueError('分页超出候选范围。')
                new=service.page(args.batch_id,args.offset)
            ledger=cls.__new__(cls);ledger.w=w;ledger.data=data
            ids=[s['id'] for s in data['subtasks'] if args.batch_id in s['batch_ids']]
            data['batches'][new['batch_id']]=new
            for s in data['subtasks']:
                s['batch_ids']=[bid for bid in s['batch_ids'] if bid in data['batches']]
                if s['id'] in ids:
                    s.update(claims=[],delivered=False)
            # Superseded scope observations must not support a future answer.
            stale={oid for oid,o in data['observations'].items()
                   if o.get('data',{}).get('batch_id')==args.batch_id} if args.frame_id else set()
            for oid in stale:data['observations'].pop(oid,None)
            for s in data['subtasks']:
                s['observation_ids']=[oid for oid in s['observation_ids'] if oid not in stale]
            for evidence in new['scope_evidence']+[i['label_evidence'] for i in new['items']]:
                data['evidence'][evidence['evidence_id']]=evidence
                for s in data['subtasks']:
                    if s['id'] in ids:s['evidence_ids']=sorted(set(s['evidence_ids'])|{evidence['evidence_id']})
            ledger.bind_batch(ids,new)
            ledger.check_batch_rows()
            from cad_agent.analysis.object_registry import record_local_batch
            record_local_batch(w,data,new)
            data['delivery_check']={'all_requirements_addressed':all(s['delivered'] for s in data['subtasks']),
                'items':[dict(requirement_id=s['id'],delivered=s['delivered'],status=s['status']) for s in data['subtasks']],
                'note':'本地续作已逐项检查；历史模型回答不随单位确认自动改写。'}
            ledger.refresh_status();ledger.save()
            return dict(task=ledger.public(),batch=new,model_calls=0,note='本地继续处理候选，已同步任务覆盖状态；未处理项目仍保留缺口。')
