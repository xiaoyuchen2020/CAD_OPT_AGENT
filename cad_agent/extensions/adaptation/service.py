"""Persistent, bounded orchestration; generated code never runs on the host."""
import hashlib
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from cad_agent.analysis.general import DrawingIndex
from cad_agent.contracts import stable_id
from cad_agent.adapters.model import ModelError
from ..table_recovery.bundle import prepare, prepare_region, digest, verify_bundle
from ..table_recovery.sandbox import SandboxError
from .contracts import SPECS
from .model import propose, check_code
from .presets import FULL_WIDTH_ROWS
from .sandbox import execute
from .validation import validate

_ROOT = Path(__file__).resolve().parent
IMPLEMENTATION_SHA256 = {str(p.relative_to(_ROOT.parent)): hashlib.sha256(p.read_bytes()).hexdigest()
    for folder in (_ROOT, _ROOT.parent/'table_recovery') for p in sorted(folder.glob('*.py'))}


def now():
    return datetime.now(timezone.utc).isoformat()


class AdaptationService:
    def __init__(self, workspace, snapshot_id):
        self.w = workspace; self.sid = snapshot_id
        with workspace.lock:
            if not hasattr(workspace, '_adaptation_jobs'):
                workspace._adaptation_jobs = {}
                for run in workspace.store.list('adaptation_run'):
                    if run['status'] in ('queued', 'running', 'cancelling'):
                        run.update(status='interrupted', error='PROCESS_RESTARTED')
                        self.save('run', run['run_id'], run)

    def save(self, kind, key, value):
        self.w.store.put('adaptation_'+kind, key, value)

    def immutable(self, kind, value):
        key = 'ad_'+kind+'_'+digest(value)
        try:
            old = self.w.store.get('adaptation_'+kind, key)
            if old != value:
                raise ValueError('ARTIFACT_INTEGRITY_FAILED')
        except KeyError:
            self.save(kind, key, value)
        return key

    def artifact(self, kind, key):
        value = self.w.store.get('adaptation_'+kind, key)
        if key != 'ad_'+kind+'_'+digest(value):
            raise ValueError('ARTIFACT_INTEGRITY_FAILED')
        return value

    def get(self, run_id):
        run = self.w.store.get('adaptation_run', run_id)
        if run['snapshot_id'] != self.sid:
            raise KeyError(run_id)
        return run

    def source_check(self, bundle):
        verify_bundle(bundle)
        if hashlib.sha256(self.w.store.artifact(self.sid, 'source.dxf').read_bytes()).hexdigest() != bundle['source_sha256']:
            raise ValueError('SOURCE_INTEGRITY_FAILED')
        for name, sha in bundle['parsed_artifact_sha256'].items():
            if name not in ('entities.json', 'regions.json', 'scene.json') or hashlib.sha256(self.w.store.artifact(self.sid, name).read_bytes()).hexdigest() != sha:
                raise ValueError('PARSED_SOURCE_INTEGRITY_FAILED')

    def start(self, args, *, background=True, shadow_policy_id=None):
        request = args.model_dump()
        if request.get('revision_of_run_id') is None:
            request.pop('revision_of_run_id', None)  # Preserve old idempotency digests.
        key = stable_id('adrun', self.sid, args.idempotency_key)
        with self.w.lock:
            try:
                existing = self.get(key)
                if existing['request_sha256'] != digest(request):
                    raise ValueError('IDEMPOTENCY_CONFLICT')
                return existing
            except KeyError:
                pass
            if any(not job['done'].is_set() for job in self.w._adaptation_jobs.values()):
                raise ValueError('ADAPTATION_BUSY')
            prior = [r for r in self.w.store.list('adaptation_run') if r['snapshot_id'] == self.sid
                     and r['request']['table_id'] == args.table_id and r['request']['method'] == args.method
                     and r['request'].get('bbox') == args.bbox and r['request'].get('layout_id') == args.layout_id]
            if len(prior) >= 6:
                raise ValueError('INPUT_ATTEMPT_LIMIT')
            if args.model_profile not in self.w.model_catalog:
                raise ValueError('UNKNOWN_MODEL_PROFILE')
            if args.mode == 'registered' and not args.method_version_id:
                raise ValueError('METHOD_VERSION_REQUIRED')
            if args.mode != 'registered' and args.method_version_id:
                raise ValueError('UNEXPECTED_METHOD_VERSION')
            if (args.mode == 'replay') != bool(args.reuse_run_id):
                raise ValueError('REPLAY_RUN_REQUIRED_OR_UNEXPECTED')
            run = dict(run_id=key, snapshot_id=self.sid, request=request,
                request_sha256=digest(request), status='queued', created_at=now(),
                attempts=[], usage={}, review_status='pending', semantic_acceptance=False,
                implementation_sha256=IMPLEMENTATION_SHA256,
                budget=dict(max_model_calls=3, max_total_tokens=96000, wall_seconds=180, sandbox_seconds=20))
            if shadow_policy_id:
                run.update(shadow_policy_id=shadow_policy_id,output_mode='shadow_only',
                           automatic_adoption=False,automatic_registration=False)
            self.save('run', key, run)
            job = dict(cancel=threading.Event(), done=threading.Event())
            self.w._adaptation_jobs[key] = job
        if background:
            self.w.executor.submit(self.work, key, job)
            return run
        self.work(key, job)
        return self.get(key)

    def cancel(self, run_id):
        with self.w.lock:
            run = self.get(run_id)
            job = self.w._adaptation_jobs.get(run_id)
            if job and not job['done'].is_set():
                job['cancel'].set()
                run['status'] = 'cancelling'; self.save('run', run_id, run)
            return run

    def work(self, run_id, job):
        run = self.get(run_id); started = time.monotonic(); args = run['request']
        deadline = started + 180; feedback = []; calls = 0; reserved = 0
        def checkpoint():
            if job['cancel'].is_set():
                raise ValueError('CANCELLED')
            if time.monotonic() >= deadline:
                raise ValueError('TIME_BUDGET_EXHAUSTED')
        try:
            run['status'] = 'running'; self.save('run', run_id, run)
            checkpoint()
            index = DrawingIndex(self.w, self.sid)
            bundle = (prepare(index, args['table_id']) if args['table_id'] else
                      prepare_region(index, args['layout_id'], args['bbox']))
            run.update(input_id=bundle['input_id'], source_sha256=bundle['source_sha256'],
                       input_artifact_id=self.immutable('input', bundle))
            self.save('run', run_id, run)
            if bundle['coverage']['blocking_diagnostics']:
                raise ValueError('INPUT_INCOMPLETE_OR_UNSUPPORTED')
            if not bundle['texts' if args['method']=='text_spans' else 'segments']:
                raise ValueError('INPUT_HAS_NO_TARGET_SOURCE')
            if args['method'] == 'table_grid' and bundle['baseline'].get('code') == 'TABLE_GRAPHICS_DOMINATE':
                raise ValueError('TABLE_IDENTITY_REQUIRES_REVIEW')
            settings = self.w.model_catalog[args['model_profile']]
            if args.get('revision_of_run_id'):
                prior = self.get(args['revision_of_run_id'])
                if (prior['request']['method'] != args['method'] or not prior['attempts']
                        or prior.get('input_id') != bundle['input_id']
                        or prior['status'] not in ('unresolved', 'rejected', 'awaiting_review', 'accepted')):
                    raise ValueError('REVISION_SOURCE_MISMATCH')
                self.source_check(self.artifact('input', prior['input_artifact_id']))
                entry = prior['attempts'][-1]
                code = self.artifact('code', entry['code_artifact_id'])
                errors = []
                if entry.get('validation_id'):
                    report = self.artifact('validation', entry['validation_id'])
                    execution = self.artifact('execution', entry['execution_id'])
                    if digest(execution['output']) != report['candidate_sha256']:
                        raise ValueError('CANDIDATE_INTEGRITY_FAILED')
                    errors = report['errors']
                feedback.append(dict(code=code['code'], errors=errors, revision_of=prior['run_id']))
                run['revision_parent_code_sha256'] = code['code_sha256']
            preset = FULL_WIDTH_ROWS if args['mode'] in ('auto', 'fixed') and args['method'] == 'table_grid' else None
            if args['mode'] == 'replay':
                prior = self.w.store.get('adaptation_run', args['reuse_run_id'])
                if prior['request']['method'] != args['method'] or not prior.get('accepted_attempt'):
                    raise ValueError('REPLAY_METHOD_MISMATCH')
                prior_service = AdaptationService(self.w, prior['snapshot_id'])
                prior_service.result(prior['run_id'])
                entry = prior['attempts'][prior['accepted_attempt']-1]
                preset = self.artifact('code', entry['code_artifact_id'])['code']
            if args['mode'] == 'registered':
                version = self.w.store.get('adaptation_version', args['method_version_id'])
                if version['status'] != 'registered' or version['method'] != args['method']:
                    raise ValueError('METHOD_NOT_REGISTERED_OR_DISABLED')
                if version['validator_version'] != SPECS[args['method']]['validator']:
                    raise ValueError('METHOD_VALIDATOR_VERSION_CHANGED')
                artifact = self.artifact('code', version['code_artifact_id'])
                preset = artifact['code']
            for attempt in range(4):
                checkpoint(); self.source_check(bundle)
                meta = dict(origin=args['mode'] if args['mode'] in ('registered','replay') else 'fixed_composition', usage={})
                if preset is not None:
                    code = preset; preset = None
                else:
                    if args['mode'] in ('registered', 'replay', 'fixed') or calls >= 3:
                        break
                    calls += 1
                    connection = {'account': self.w.siwc_account} if settings.provider == 'responses_subscription' else {}
                    proposal, meta = propose(settings, args['method'], args['instruction'], bundle,
                                             feedback, timeout=min(55, deadline-time.monotonic()), remaining_tokens=96000-reserved,
                                             **connection)
                    reserved += meta['reserved_tokens']
                    if 'prompt_messages' in meta:
                        meta['prompt_artifact_id'] = self.immutable('prompt', dict(messages=meta.pop('prompt_messages')))
                    meta['origin'] = 'llm'
                    for key, value in meta['usage'].items():
                        run['usage'][key] = run['usage'].get(key, 0) + value
                    proposal_id = self.immutable('proposal', dict(proposal=proposal.model_dump(), metadata=meta,
                        input_id=bundle['input_id'], instruction=args['instruction']))
                    meta['proposal_id'] = proposal_id
                    if proposal.strategy == 'abstain':
                        run.update(status='unresolved', abstention=proposal.explanation); break
                    code = proposal.code
                checkpoint()
                code_sha = hashlib.sha256(code.encode()).hexdigest()
                code_id = self.immutable('code', dict(code=code, code_sha256=code_sha, method=args['method']))
                entry = dict(number=attempt+1, code_artifact_id=code_id, code_sha256=code_sha, **meta)
                run['attempts'].append(entry); self.save('run', run_id, run)
                try:
                    check_code(code)
                except (ValueError, SyntaxError, RecursionError):
                    entry['error'] = 'INVALID_CODE_INTERFACE_OR_SYNTAX'
                    feedback.append(dict(code=code, errors=[entry['error']]))
                    self.save('run', run_id, run)
                    continue
                try:
                    execution = execute(self.w.settings.data_dir, bundle, code, cancel=job['cancel'],
                                        timeout=min(20, deadline-time.monotonic()))
                    checkpoint(); self.source_check(bundle)
                    entry['execution_id'] = self.immutable('execution', execution)
                    validation = validate(args['method'], bundle, execution['output'])
                    checkpoint()
                    entry.update(validation_id=self.immutable('validation', validation),
                        validation_status=validation['validation_status'], errors=validation['errors'])
                    if validation['validation_status'] == 'passed':
                        run.update(status='awaiting_review', candidate_sha256=validation['candidate_sha256'],
                                   accepted_attempt=attempt+1)
                        run['experimental_method_id'] = self.immutable('version', dict(
                            name=args['method']+'-'+code_sha[:12], code_artifact_id=code_id,
                            method=args['method'], validator_version=SPECS[args['method']]['validator'],
                            status='experimental', automatic_promotion=False, semantic_acceptance=False))
                        break
                    feedback.append(dict(code=code, errors=validation['errors'], warnings=validation.get('warnings', [])))
                except SandboxError as exc:
                    entry['error'] = exc.code
                    if exc.code in ('SANDBOX_CANCELLED', 'SANDBOX_UNAVAILABLE', 'SANDBOX_ISOLATION_MISMATCH'):
                        raise
                    feedback.append(dict(code=code, errors=[exc.code]))
                self.save('run', run_id, run)
            if run['status'] == 'running':
                run['status'] = 'unresolved'
        except (ValueError, ModelError, SandboxError) as exc:
            error = getattr(exc, 'code', str(exc))
            if isinstance(error, str):
                error = error.split(':', 1)[0]
            run.update(status='cancelled' if job['cancel'].is_set() else 'refused',
                       error=error if isinstance(error,str) and error.isupper() and len(error)<100 else 'ADAPTATION_INVALID_PROPOSAL')
        except Exception:
            run.update(status='failed', error='ADAPTATION_INTERNAL_ERROR')
        finally:
            if job['cancel'].is_set():
                run.update(status='cancelled')
            run.update(elapsed_seconds=round(time.monotonic()-started, 3), completed_at=now(), model_calls=calls,
                       reserved_tokens=reserved)
            self.save('run', run_id, run); job['done'].set()

    def result(self, run_id):
        run = self.get(run_id)
        if not run.get('accepted_attempt'):
            raise ValueError('NO_VALIDATED_CANDIDATE')
        entry = run['attempts'][run['accepted_attempt']-1]
        if run['status'] not in ('awaiting_review', 'accepted', 'rejected'):
            raise ValueError('CANDIDATE_NOT_REVIEWABLE')
        bundle = self.artifact('input', run['input_artifact_id']); self.source_check(bundle)
        report = self.artifact('validation', entry['validation_id'])
        execution = self.artifact('execution', entry['execution_id'])
        self.artifact('code', entry['code_artifact_id'])
        if digest(execution['output']) != report['candidate_sha256']:
            raise ValueError('CANDIDATE_INTEGRITY_FAILED')
        return dict(run=run, validation=report, bundle=bundle)

    def review(self, run_id, args):
        with self.w.lock:
            result = self.result(run_id); run = result['run']
            if run['status'] != 'awaiting_review' or run['candidate_sha256'] != args.candidate_sha256:
                raise ValueError('STALE_OR_ALREADY_REVIEWED_CANDIDATE')
            if args.scope != SPECS[run['request']['method']]['review_scope']:
                raise ValueError('REVIEW_SCOPE_MISMATCH')
            review = dict(run_id=run_id, **args.model_dump(), created_at=now(),
                          source_sha256=run['source_sha256'], reviewer='local_user')
            run.update(review_id=self.immutable('review', review), review_status=args.decision,
                       status='accepted' if args.decision == 'accepted' else 'rejected')
            self.save('run', run_id, run)
            return run

    def promote(self, args):
        runs = [self.w.store.get('adaptation_run', rid) for rid in args.run_ids]
        if any(r.get('output_mode')=='shadow_only' for r in runs):
            raise ValueError('SHADOW_RUN_NOT_PROMOTABLE')
        if len({r['source_sha256'] for r in runs}) < 2 or any(r['status'] != 'accepted' for r in runs):
            raise ValueError('ACCEPTED_CROSS_DRAWING_REGRESSION_REQUIRED')
        entries = [r['attempts'][r['accepted_attempt']-1] for r in runs]
        for r in runs:
            owner = AdaptationService(self.w, r['snapshot_id'])
            owner.result(r['run_id'])
            review = owner.artifact('review', r['review_id'])
            if review['decision'] != 'accepted' or review['candidate_sha256'] != r['candidate_sha256']:
                raise ValueError('REVIEW_INTEGRITY_FAILED')
        if len({e['code_artifact_id'] for e in entries}) != 1 or len({r['request']['method'] for r in runs}) != 1:
            raise ValueError('SAME_METHOD_AND_CODE_REGRESSION_REQUIRED')
        value = dict(name=args.name, code_artifact_id=entries[0]['code_artifact_id'],
            method=runs[0]['request']['method'], regression_run_ids=args.run_ids,
            validator_version=SPECS[runs[0]['request']['method']]['validator'],
            status='registered', registered_at=now(), scope='reviewed_local_regions',
            cross_project_generalization=False, semantic_acceptance=False)
        key = self.immutable('version', value)
        return dict(method_version_id=key, **value)

    def disable(self, version_id):
        value = self.w.store.get('adaptation_version', version_id)
        self.immutable('version_event', dict(version_id=version_id, action='disabled', at=now()))
        value['status'] = 'disabled'; self.save('version', version_id, value)
        return value
