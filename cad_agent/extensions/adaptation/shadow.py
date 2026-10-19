"""Host-configured multi-method trials. Candidates never become Agent evidence."""
from datetime import datetime, timedelta, timezone
from typing import Literal
from pydantic import Field, model_validator
from cad_agent.contracts import Record, FiniteFloat, stable_id
from .contracts import Start, Family
from .capabilities import TRIGGERS
from .service import AdaptationService, now
from ..table_recovery.bundle import digest


def destination(settings):
    from cad_agent.adapters.siwc import RESOURCE
    return RESOURCE if settings.provider == 'responses_subscription' else settings.api_base


class Target(Record):
    snapshot_id: str
    table_id: str | None = None
    diagnostic_run_id: str | None = None
    method: Family = 'table_grid'
    layout_id: str | None = None
    bbox: list[FiniteFloat] | None = Field(default=None, min_length=4, max_length=4)
    instruction: str | None = Field(default=None, min_length=1, max_length=2000)
    reuse_run_id: str | None = None
    trigger: Literal['host_failure', 'authorized_read'] = 'host_failure'
    trigger_tools: list[str] | None = Field(default=None, min_length=1, max_length=10)

    @model_validator(mode='after')
    def scope(self):
        Start(table_id=self.table_id, layout_id=self.layout_id, bbox=self.bbox,
              method=self.method, idempotency_key='validate-scope')
        if self.bbox is not None and (self.bbox[0] >= self.bbox[2] or self.bbox[1] >= self.bbox[3]):
            raise ValueError('INVALID_SCOPE_BOUNDS')
        if self.trigger_tools is not None and (len(set(self.trigger_tools)) != len(self.trigger_tools)
                or not set(self.trigger_tools) <= set(TRIGGERS[self.method])):
            raise ValueError('UNSUPPORTED_METHOD_TRIGGER')
        return self

    def request(self):
        value=self.model_dump()
        # Keep the exact v1 request shape for old table policies/idempotency keys.
        defaults=dict(method='table_grid',layout_id=None,bbox=None,instruction=None,
                      reuse_run_id=None,trigger='host_failure',trigger_tools=None)
        for k,v in defaults.items():
            if value[k]==v:value.pop(k)
        return value


def target_key(t):
    scope=t.get('table_id') or [t.get('layout_id'),t.get('bbox')]
    return stable_id('adtarget',t['snapshot_id'],t.get('method','table_grid'),digest(scope))


def run_key(policy_id,t):
    # V1 table idempotency remains unchanged; different methods never collide.
    scope=t['table_id'] if t.get('method','table_grid')=='table_grid' else target_key(t)
    return stable_id('shadow',policy_id,t['snapshot_id'],scope)


def observation_key(policy_id,t):
    scope=t['table_id'] if t.get('method','table_grid')=='table_grid' else target_key(t)
    return stable_id('adobserve',policy_id,t['snapshot_id'],scope)


class Policy(Record):
    idempotency_key: str = Field(min_length=1, max_length=100)
    targets: list[Target] = Field(min_length=1, max_length=100)
    model_profile: str = 'siwc_sol'
    mode: Literal['dynamic', 'replay'] = 'replay'
    reuse_run_id: str | None = None
    instruction: str = Field(min_length=1, max_length=2000)
    max_runs: int = Field(default=24, ge=1, le=100)
    max_model_calls: int = Field(default=0, ge=0, le=30)
    max_reserved_tokens: int = Field(default=0, ge=0, le=960000)
    lifetime_seconds: int = Field(default=14400, ge=60, le=86400)
    agent_shadow_enabled: bool = False


class ShadowService:
    def __init__(self, workspace):self.w=workspace

    def create(self,args):
        spec=args.model_dump();spec['targets']=[t.request() for t in args.targets]
        key=stable_id('adpolicy',args.idempotency_key)
        with self.w.lock:
            try:
                old=self.get(key)
                if old['request_sha256']!=digest(spec):raise ValueError('POLICY_IDEMPOTENCY_CONFLICT')
                return old
            except KeyError:pass
            if args.mode=='dynamic' and (args.reuse_run_id or any(t.reuse_run_id for t in args.targets)):
                raise ValueError('POLICY_REPLAY_MODE_REQUIRED')
            if args.mode=='dynamic' and (args.max_model_calls<3 or args.max_reserved_tokens<96000):
                raise ValueError('POLICY_DYNAMIC_RESERVATION_REQUIRED')
            settings=self.w.model_catalog.get(args.model_profile)
            if settings is None:raise ValueError('UNKNOWN_MODEL_PROFILE')
            targets=[]
            for t in args.targets:
                d=self.w.store.get('drawing',t.snapshot_id)
                if t.layout_id and t.layout_id not in {v['layout_id'] for v in d['layouts']}:
                    raise ValueError('LAYOUT_NOT_IN_SNAPSHOT')
                target=dict(**t.request(),source_sha256=d['sha256'])
                target['target_id']=target_key(target);targets.append(target)
                if args.mode=='replay':
                    origin=t.reuse_run_id or args.reuse_run_id
                    if not origin:raise ValueError('POLICY_REPLAY_REQUIRED')
                    r=self.w.store.get('adaptation_run',origin)
                    if r['request']['method']!=t.method:raise ValueError('REPLAY_METHOD_MISMATCH')
                    AdaptationService(self.w,r['snapshot_id']).result(r['run_id'])
            if len({t['target_id'] for t in targets})!=len(targets):raise ValueError('DUPLICATE_POLICY_TARGET')
            policy=dict(policy_id=key,request_sha256=digest(spec),spec=spec,targets=targets,
                endpoint=destination(settings),model=settings.model,provider=settings.provider,created_at=now(),
                expires_at=(datetime.now(timezone.utc)+timedelta(seconds=args.lifetime_seconds)).isoformat(),
                output_mode='shadow_only',automatic_adoption=False,automatic_registration=False)
            policy['seal']=digest(policy)
            self.w.store.put('adaptation_policy',key,policy)
            self.w.store.put('adaptation_policy_ledger',key+'-ledger',dict(enabled=True,runs={},reserved_model_calls=0,reserved_tokens=0))
            return policy

    def get(self,policy_id):
        p=self.w.store.get('adaptation_policy',policy_id)
        if p['seal']!=digest({k:v for k,v in p.items() if k!='seal'}):raise ValueError('POLICY_INTEGRITY_FAILED')
        return p

    def disable(self,policy_id):
        self.get(policy_id)
        with self.w.lock:
            ledger=self.w.store.get('adaptation_policy_ledger',policy_id+'-ledger')
            ledger.update(enabled=False,disabled_at=now())
            self.w.store.put('adaptation_policy_ledger',policy_id+'-ledger',ledger)
        return dict(policy_id=policy_id,enabled=False)

    def _active(self,p):
        ledger=self.w.store.get('adaptation_policy_ledger',p['policy_id']+'-ledger')
        if not ledger['enabled'] or datetime.now(timezone.utc)>=datetime.fromisoformat(p['expires_at']):
            raise ValueError('SHADOW_POLICY_DISABLED_OR_EXPIRED')
        settings=self.w.model_catalog[p['spec']['model_profile']]
        if (destination(settings),settings.model,settings.provider)!=(p['endpoint'],p['model'],p['provider']):
            raise ValueError('POLICY_MODEL_CHANGED')
        return ledger

    def _target(self,p,sid,table_id=None,target_id=None):
        if bool(table_id)==bool(target_id):raise ValueError('ONE_TARGET_SELECTOR_REQUIRED')
        target=next((t for t in p['targets'] if t['snapshot_id']==sid and
            ((target_id and target_key(t)==target_id) or (table_id and t.get('table_id')==table_id and t.get('method','table_grid')=='table_grid'))),None)
        if target is None:raise ValueError('SHADOW_SCOPE_DENIED')
        if self.w.store.get('drawing',sid)['sha256']!=target['source_sha256']:raise ValueError('SOURCE_INTEGRITY_FAILED')
        return target

    def trial(self,policy_id,sid,table_id=None,*,target_id=None,background=True,initiator='user_trial'):
        p=self.get(policy_id);t=self._target(p,sid,table_id,target_id);spec=p['spec']
        service=AdaptationService(self.w,sid);key=run_key(policy_id,t)
        args=Start(table_id=t.get('table_id'),layout_id=t.get('layout_id'),bbox=t.get('bbox'),
            method=t.get('method','table_grid'),mode=spec['mode'],model_profile=spec['model_profile'],
            instruction=t.get('instruction') or spec['instruction'],idempotency_key=key,
            reuse_run_id=t.get('reuse_run_id') or spec['reuse_run_id'],
            revision_of_run_id=t.get('diagnostic_run_id') if spec['mode']=='dynamic' else None)
        with self.w.lock:
            ledger=self._active(p)
            if key not in ledger['runs']:
                calls,tokens=(3,96000) if spec['mode']=='dynamic' else (0,0)
                if (len(ledger['runs'])>=spec['max_runs'] or ledger['reserved_model_calls']+calls>spec['max_model_calls']
                        or ledger['reserved_tokens']+tokens>spec['max_reserved_tokens']):
                    raise ValueError('SHADOW_POLICY_BUDGET_EXHAUSTED')
                ledger['runs'][key]=dict(snapshot_id=sid,target_id=target_key(t),table_id=t.get('table_id'),
                    method=args.method,initiator=initiator,reserved_at=now())
                ledger['reserved_model_calls']+=calls;ledger['reserved_tokens']+=tokens
                self.w.store.put('adaptation_policy_ledger',policy_id+'-ledger',ledger)
        run=service.start(args,background=background,shadow_policy_id=policy_id)
        event=dict(policy_id=policy_id,run_id=run['run_id'],snapshot_id=sid,target_id=target_key(t),
            table_id=t.get('table_id'),method=args.method,layout_id=t.get('layout_id'),bbox=t.get('bbox'),
            initiator=ledger['runs'][key]['initiator'],mode='shadow_only',automatic_adoption=False,
            automatic_registration=False,created_at=ledger['runs'][key]['reserved_at'])
        self.w.store.put('adaptation_shadow_event',key,event)
        return event

    def observe(self,policy_id,sid,table_id=None,*,target_id=None,background=True):
        p=self.get(policy_id);t=self._target(p,sid,table_id,target_id)
        if not p['spec']['agent_shadow_enabled']:return dict(status='disabled')
        self._active(p);method=t.get('method','table_grid');key=observation_key(policy_id,t)
        try:
            previous=self.w.store.get('adaptation_shadow_observation',key)
            if previous.get('status')=='queued':return previous
        except KeyError:pass
        from cad_agent.analysis.general import DrawingIndex
        from ..table_recovery.bundle import prepare,prepare_region
        index=DrawingIndex(self.w,sid)
        bundle=prepare(index,t['table_id']) if t.get('table_id') else prepare_region(index,t['layout_id'],t['bbox'])
        if bundle['coverage']['blocking_diagnostics']:return dict(status='skipped',reason='INPUT_INCOMPLETE_OR_UNSUPPORTED')
        if not bundle['texts' if method=='text_spans' else 'segments']:return dict(status='skipped',reason='INPUT_HAS_NO_TARGET_SOURCE')
        baseline=bundle['baseline'];reason=baseline.get('code') if method=='table_grid' else None
        if reason=='TABLE_GRAPHICS_DOMINATE':return dict(status='skipped',reason='NON_TABLE')
        if reason=='ANALYSIS_LIMIT_EXCEEDED':return dict(status='skipped',reason=reason)
        eligible=method=='table_grid' and baseline.get('status')=='unresolved'
        if t.get('diagnostic_run_id'):
            service=AdaptationService(self.w,sid);prior=service.get(t['diagnostic_run_id'])
            if prior.get('input_id')!=bundle['input_id'] or prior['request']['method']!=method:
                raise ValueError('DIAGNOSTIC_SOURCE_OR_METHOD_MISMATCH')
            for a in prior['attempts']:
                if a.get('validation_id'):
                    report=service.artifact('validation',a['validation_id']);execution=service.artifact('execution',a['execution_id'])
                    if digest(execution['output'])!=report['candidate_sha256']:raise ValueError('CANDIDATE_INTEGRITY_FAILED')
                    if report['validation_status']=='rejected':eligible=True;reason='VALIDATED_METHOD_GAP'
        if t.get('trigger')=='authorized_read':eligible=True;reason='HOST_AUTHORIZED_READ_TRIAL'
        if not eligible:return dict(status='skipped',reason='NO_HOST_CONFIRMED_GAP')
        event=self.trial(policy_id,sid,target_id=target_key(t),initiator='agent_shadow',background=background)
        return dict(status='queued',reason=reason,**event)
